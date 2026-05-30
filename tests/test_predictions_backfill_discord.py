"""Spec C commit #7 — Discord 14-day backfill tests.

Covers ``argosy/services/predictions/discord_backfill.py`` +
``argosy/orchestrator/loops/predictions_backfill_discord.py``.

Test surface (per the commit prompt):

  * Happy path — 30 messages, 8 parseable → 8 predictions written.
  * Pagination — 100-msg pages, ``before`` cursor honored, walk
    terminates when oldest message is beyond lookback window.
  * Idempotency — re-running with the same set → 0 NEW, all deduped
    via the writer's per-source ``(source, message_id)`` contract.
  * Rate-limit — 429 with ``Retry-After`` honored + retried.
  * Missing creds — graceful error in summary (NOT exception); 0 rows
    written; no JobRegistry tick crash.
  * Manual trigger via :meth:`JobRegistry.fire_now` runs the backfill
    end-to-end.
  * Hindsight-bias — ``event_at`` on the written prediction equals
    the message timestamp, NOT backfill-run time.

Test command::

    D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest \\
        -m "not llm_eval" tests/test_predictions_backfill_discord.py -v
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.orchestrator.loops.predictions_backfill_discord import (
    PredictionsBackfillDiscordLoop,
    predictions_backfill_discord_metadata,
)
from argosy.services.predictions.discord_backfill import (
    BackfillSummary,
    _fetch_page,
    backfill_discord_predictions,
)
from argosy.state.models import Base, Prediction, User


USER = "ariel"
CHANNEL_ID = 1234567890
BOT_TOKEN = "MTk1NDg2NDU0OTQ4OTQ5MTI0.GExample.token_value_padding_xyz"

# Fixed "now" for the lookback math — all message-timestamp fixtures
# below are relative to this so the 14-day window is deterministic.
NOW = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)

# Pool of 1-5 uppercase tickers (real US-equity-shape) the parser accepts.
# Using stable real-ish tickers so the parser's regex
# (``[A-Z]{1,5}`` only — no digits) accepts them.
_TICKER_POOL = [
    "NVDA", "AMD", "TSLA", "AAPL", "MSFT", "GOOG", "META", "ORCL",
    "AMZN", "SHOP", "PYPL", "IBM", "WMT", "DIS", "INTC", "QCOM",
    "ADBE", "CRM", "NFLX", "BABA", "UBER", "LYFT", "TWLO", "DDOG",
]


# ---------------------------------------------------------------------------
# DB fixture — mirrors tests/test_predictions_writers.py::sync_session
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    """File-backed SQLite session with the predictions schema in place.

    Mirrors :func:`tests.test_predictions_writers.sync_session` so the
    writer's UNIQUE-index contract and FK-into-registry contract both
    fire identically to production.
    """
    db_path = tmp_path / "predictions_backfill.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @sa.event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover — connect hook
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        try:
            conn.execute(
                sa.text(
                    "DROP INDEX IF EXISTS ix_predictions_source_messageid"
                )
            )
        except Exception:  # pragma: no cover — defensive
            pass
        conn.execute(
            sa.text(
                "CREATE UNIQUE INDEX ix_predictions_source_messageid "
                "ON predictions (source, message_id) "
                "WHERE message_id IS NOT NULL"
            )
        )
        for method_name, family in (
            ("target_stop", "target_stop"),
            ("fixed_lookahead_7d", "fixed_lookahead"),
            ("fixed_lookahead_30d", "fixed_lookahead"),
            ("multi_basket_weighted", "multi_basket"),
            ("unparseable", "unparseable"),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO evaluation_method_registry "
                    "(method_name, family, method_version, is_active) "
                    "VALUES (:m, :f, 1, 1)"
                ),
                {"m": method_name, "f": family},
            )

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    db.add(User(id=USER, plan="free"))
    db.commit()
    try:
        yield db, SessionLocal, str(db_path)
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Discord message fixture helpers
# ---------------------------------------------------------------------------


def _msg(
    msg_id: str,
    ts: datetime,
    content: str,
) -> dict[str, Any]:
    """Build a Discord-shaped message JSON object."""
    iso = ts.astimezone(timezone.utc).isoformat()
    return {
        "id": msg_id,
        "channel_id": str(CHANNEL_ID),
        "content": content,
        "timestamp": iso,
        "author": {"id": "1", "username": "trader"},
    }


def _mk_stub_fetcher(pages: list[list[dict[str, Any]]]):
    """Build a ``page_fetcher`` stub that returns ``pages`` in order.

    Each call returns the next page from the list; running out returns
    an empty list (Discord's "end of history" signal).
    """
    pages_iter = iter(pages)

    async def _stub(
        channel_id: int,
        before_id: str | None,
        bot_token: str,
    ) -> list[dict[str, Any]]:
        try:
            return next(pages_iter)
        except StopIteration:
            return []

    return _stub


def _now() -> datetime:
    return NOW


# ---------------------------------------------------------------------------
# Service-level: happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_30_messages_8_parseable(sync_session):
    """30 messages — 8 contain a parseable alpha call — 8 predictions
    written (the rest skipped silently as unparseable chatter).
    """
    db, _, _ = sync_session

    parseable_lines = [
        f"BUY ${_TICKER_POOL[i]} → $180 stop $135" for i in range(8)
    ]
    chatter_lines = [
        "lol nice", "good morning", "what's the market doing today?",
        "anyone else worried about CPI?", "thinking about lunch",
        "did you see that meme?", "just chillin",
        "the weather is nice", "tax season is brutal",
        "happy birthday Sarah",
        "macros looking weird", "is the dollar strong today?",
        "I prefer green tea", "yet another wrench", "carry on",
        "ok bye for now", "back later", "tip of the hat",
        "lunch was great", "no specific tickers here",
        "general musings", "philosophical question",
    ]
    msgs = []
    base_ts = NOW - timedelta(days=1)  # all within 14-day lookback
    for i in range(30):
        body = (
            parseable_lines[i] if i < 8 else chatter_lines[i - 8]
        )
        msgs.append(
            _msg(
                msg_id=f"msg-{i:04d}",
                ts=base_ts - timedelta(minutes=i),
                content=body,
            )
        )

    # Stub returns the 30 messages in one page; subsequent calls
    # return empty (end of channel history). The walk will fetch the
    # first page, then ask for a second page to confirm there's no
    # more in-window history — that empty page terminates the walk.
    fetcher = _mk_stub_fetcher([msgs])

    summary = await backfill_discord_predictions(
        db,
        lookback_days=14,
        channel_id=CHANNEL_ID,
        bot_token=BOT_TOKEN,
        page_fetcher=fetcher,
        now=_now,
    )

    assert isinstance(summary, BackfillSummary)
    assert summary.messages_scanned == 30
    assert summary.predictions_written == 8
    assert summary.predictions_deduped == 0
    assert summary.messages_unparseable == 22
    assert summary.errors == []
    # Page 1 had all-in-window messages, so the walker asks for a
    # page 2 (empty) to confirm there's nothing else within lookback.
    assert summary.pages_fetched == 2

    # Verify the rows landed.
    count = db.scalar(
        sa.select(sa.func.count(Prediction.id)).where(
            Prediction.source == "discord"
        )
    )
    assert count == 8


# ---------------------------------------------------------------------------
# Service-level: pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pagination_walks_until_lookback_exhausted(sync_session):
    """Two pages: page 1 = 100 messages all within window; page 2 =
    100 messages straddling the cutoff. Walk stops after page 2
    (oldest in page 2 is beyond cutoff) — does NOT fetch a 3rd page.
    """
    db, _, _ = sync_session

    page1 = []
    # Page 1: 100 parseable messages all within the last 7 days.
    # 5-minute spacing so all 100 fit in a 7-day window.
    base = NOW - timedelta(days=1)
    for i in range(100):
        page1.append(
            _msg(
                msg_id=f"p1-{i:04d}",
                ts=base - timedelta(minutes=i * 5),
                content=f"BUY ${_TICKER_POOL[i % len(_TICKER_POOL)]} → $100 stop $80",
            )
        )

    # Page 2: 100 messages — first 20 within lookback (~13d, hours),
    # remaining 80 beyond it (day 14+). The oldest in this page is
    # well beyond cutoff so the walk terminates AFTER consuming
    # page 2.
    page2_start = NOW - timedelta(days=13, hours=23)
    page2 = []
    for i in range(100):
        page2.append(
            _msg(
                msg_id=f"p2-{i:04d}",
                ts=page2_start - timedelta(hours=i),
                content=f"LONG ${_TICKER_POOL[i % len(_TICKER_POOL)]} target $200",
            )
        )

    # Track what cursor was passed each call.
    cursors_seen: list[str | None] = []

    async def fetcher(channel_id, before_id, bot_token):
        cursors_seen.append(before_id)
        if before_id is None:
            return page1
        if before_id == "p1-0099":
            return page2
        # If the walk asks for a 3rd page, that's a bug — but we
        # serve an empty list so the bug becomes a quiet PASS-ish
        # rather than a hang.
        return []

    summary = await backfill_discord_predictions(
        db,
        lookback_days=14,
        channel_id=CHANNEL_ID,
        bot_token=BOT_TOKEN,
        page_fetcher=fetcher,
        now=_now,
    )

    assert summary.pages_fetched == 2
    # First call had no cursor (newest), second walked from oldest
    # in page 1.
    assert cursors_seen == [None, "p1-0099"]
    # All 100 page-1 messages + the 24 in-window page-2 messages
    # (the first 24 hours of page 2 fit in the 14-day window).
    # page2_start = NOW - 13d 23h; cutoff = NOW - 14d.
    # In-window count = floor((23 + cutoff_to_page2_start) hrs) + 1.
    # = floor((23 + 1)) + 1 = 25 hours -> 25 messages? Let me just
    # accept 100 + the actual in-window count (computed below).
    in_window_page2 = sum(
        1 for _i in range(100)
        if (page2_start - timedelta(hours=_i)) >= NOW - timedelta(days=14)
    )
    assert summary.predictions_written == 100 + in_window_page2
    # Page 2 also scanned the 100 - in_window_page2 out-of-window
    # messages (counters see every message even if not written).
    assert summary.messages_scanned == 200


@pytest.mark.asyncio
async def test_pagination_cursor_uses_page_tail_not_min_parseable_ts(
    sync_session,
):
    """Codex review IMPORTANT 1 — regression test.

    If the OLDEST message in a page (i.e. ``page[-1]``) is malformed
    (missing/un-parseable timestamp) the cursor for the NEXT page
    must still be derived from ``page[-1]["id"]``, NOT from the
    youngest-parseable-timestamp position. Otherwise the cursor
    advances to a younger id and the next page re-fetches messages
    we already consumed, inflating ``messages_scanned`` and risking
    an infinite same-cursor loop if the malformed tail keeps
    reappearing.
    """
    db, _, _ = sync_session

    base = NOW - timedelta(hours=1)
    page_with_bad_tail = [
        _msg(
            msg_id=f"good-{i}",
            ts=base - timedelta(minutes=i),
            content=f"BUY ${_TICKER_POOL[i]} target 200 stop 150",
        )
        for i in range(3)
    ]
    # Tail of page is malformed — timestamp field broken. The cursor
    # MUST still be ``bad-tail``'s id (the real ``page[-1]["id"]``),
    # not ``good-2`` (the oldest parseable id by min-timestamp).
    page_with_bad_tail.append(
        {
            "id": "bad-tail",
            "channel_id": str(CHANNEL_ID),
            "content": "garbage",
            "timestamp": "NOT-AN-ISO-TIMESTAMP",
        }
    )

    cursors_seen: list[str | None] = []

    async def fetcher(channel_id, before_id, bot_token):
        cursors_seen.append(before_id)
        if before_id is None:
            return page_with_bad_tail
        return []  # signal end of channel history

    summary = await backfill_discord_predictions(
        db,
        lookback_days=14,
        channel_id=CHANNEL_ID,
        bot_token=BOT_TOKEN,
        page_fetcher=fetcher,
        now=_now,
    )

    # Cursor advance MUST be the page's last-by-position id, NOT the
    # oldest-parseable-timestamp's id.
    assert cursors_seen == [None, "bad-tail"]
    # The 3 good messages were written; the bad tail counted as
    # malformed (not unparseable-by-parser).
    assert summary.predictions_written == 3
    assert any("malformed_message" in e for e in summary.errors)


@pytest.mark.asyncio
async def test_pagination_terminates_when_channel_exhausted(sync_session):
    """Discord returns an empty list when there's no more history;
    walk must stop without infinite-looping or raising.
    """
    db, _, _ = sync_session

    only_page = [
        _msg(
            msg_id="m-0",
            ts=NOW - timedelta(hours=1),
            content="BUY $NVDA → 180 stop 135",
        ),
    ]
    fetcher = _mk_stub_fetcher([only_page])

    summary = await backfill_discord_predictions(
        db,
        lookback_days=14,
        channel_id=CHANNEL_ID,
        bot_token=BOT_TOKEN,
        page_fetcher=fetcher,
        now=_now,
    )

    # Page 1 had data, page 2 was empty → stop. Two total fetches.
    assert summary.pages_fetched == 2
    assert summary.predictions_written == 1
    assert summary.errors == []


# ---------------------------------------------------------------------------
# Service-level: idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_re_run_dedups_via_writer(sync_session):
    """Run the backfill, then re-run with the SAME message set →
    0 new predictions, all deduped via the writer's per-source
    ``(source, message_id)`` contract.
    """
    db, _, _ = sync_session

    msgs = [
        _msg(
            msg_id=f"dup-{i}",
            ts=NOW - timedelta(hours=i + 1),
            content=f"BUY ${_TICKER_POOL[i]} → $200 stop $150",
        )
        for i in range(5)
    ]

    # First run — fresh writes.
    summary_first = await backfill_discord_predictions(
        db,
        lookback_days=14,
        channel_id=CHANNEL_ID,
        bot_token=BOT_TOKEN,
        page_fetcher=_mk_stub_fetcher([msgs]),
        now=_now,
    )
    assert summary_first.predictions_written == 5
    assert summary_first.predictions_deduped == 0

    # Second run — same messages, fresh fetcher (the previous one
    # was exhausted by the first run).
    summary_second = await backfill_discord_predictions(
        db,
        lookback_days=14,
        channel_id=CHANNEL_ID,
        bot_token=BOT_TOKEN,
        page_fetcher=_mk_stub_fetcher([msgs]),
        now=_now,
    )
    assert summary_second.predictions_written == 0
    assert summary_second.predictions_deduped == 5

    # Total rows in DB stays 5 — no duplicates.
    count = db.scalar(
        sa.select(sa.func.count(Prediction.id)).where(
            Prediction.source == "discord"
        )
    )
    assert count == 5


# ---------------------------------------------------------------------------
# Service-level: rate-limit handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_429_sleeps_then_retries():
    """Mock a 429 response on the first request; second responds 200.
    The fetcher must sleep ``Retry-After`` then return the second
    response's body.
    """
    call_count = {"n": 0}
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    payload_200 = [
        {
            "id": "abc",
            "channel_id": str(CHANNEL_ID),
            "content": "hello",
            "timestamp": NOW.isoformat(),
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "2.5"},
                json={"message": "You are being rate limited."},
            )
        return httpx.Response(200, json=payload_200)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _fetch_page(
            CHANNEL_ID,
            before_id=None,
            bot_token=BOT_TOKEN,
            client=client,
            sleep=fake_sleep,
        )

    assert call_count["n"] == 2
    assert sleep_calls == [2.5]
    assert result == payload_200


@pytest.mark.asyncio
async def test_rate_limit_sustained_429_raises():
    """Two 429s in a row → escalate as ``HTTPStatusError`` so the caller
    surfaces the throttle to the operator UI.
    """
    async def fake_sleep(s: float) -> None:  # noqa: ARG001
        return

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "1"},
            json={"message": "rate limited"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await _fetch_page(
                CHANNEL_ID,
                before_id=None,
                bot_token=BOT_TOKEN,
                client=client,
                sleep=fake_sleep,
            )
        assert exc_info.value.response.status_code == 429


# ---------------------------------------------------------------------------
# Service-level: missing creds → graceful error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_creds_returns_error_no_rows(
    sync_session, tmp_path, monkeypatch
):
    """No creds file → summary has an ``errors`` entry; no rows
    written; no exception.

    We patch the discord_listener's ``_default_creds_path`` (which
    is what ``load_creds()`` consults) so the load returns ``None``
    cleanly.
    """
    db, _, _ = sync_session
    fake_creds_path = tmp_path / "no_such_file.json"
    assert not fake_creds_path.exists()

    # Patch the canonical creds-path source so ``load_creds`` returns
    # ``None`` (file-not-found path).
    from argosy.services import discord_listener as dl_mod
    from argosy.services.predictions import discord_backfill as bf_mod

    monkeypatch.setattr(
        dl_mod, "_default_creds_path", lambda: fake_creds_path
    )
    # Also patch the bookkeeping-only reference in the backfill
    # module so ``summary.creds_path`` reads the test path.
    monkeypatch.setattr(
        bf_mod, "_default_creds_path", lambda: fake_creds_path
    )

    summary = await backfill_discord_predictions(
        db,
        lookback_days=14,
        # NOT passing channel_id/bot_token → forces creds-file load
        page_fetcher=_mk_stub_fetcher([]),
        now=_now,
    )

    assert summary.predictions_written == 0
    assert summary.pages_fetched == 0
    assert summary.errors  # non-empty
    assert any("creds_missing" in e for e in summary.errors)
    assert summary.creds_path == str(fake_creds_path)

    # No rows written.
    count = db.scalar(
        sa.select(sa.func.count(Prediction.id)).where(
            Prediction.source == "discord"
        )
    )
    assert count == 0


# ---------------------------------------------------------------------------
# Service-level: hindsight-bias canary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_at_is_message_timestamp_not_backfill_run_time(
    sync_session,
):
    """Hindsight-bias killer — the written prediction's ``event_at``
    must equal the Discord message's timestamp (10 days old), NOT
    the backfill-run wallclock (NOW).
    """
    db, _, _ = sync_session

    old_ts = NOW - timedelta(days=10, hours=4)
    msgs = [
        _msg(
            msg_id="historical",
            ts=old_ts,
            content="BUY $NVDA → 180 stop 135",
        ),
    ]
    fetcher = _mk_stub_fetcher([msgs])

    await backfill_discord_predictions(
        db,
        lookback_days=14,
        channel_id=CHANNEL_ID,
        bot_token=BOT_TOKEN,
        page_fetcher=fetcher,
        now=_now,
    )

    row = db.scalar(sa.select(Prediction).where(Prediction.ticker == "NVDA"))
    assert row is not None
    # SQLite strips tzinfo on roundtrip — re-attach for comparison.
    stored_event_at = row.event_at
    if stored_event_at.tzinfo is None:
        stored_event_at = stored_event_at.replace(tzinfo=timezone.utc)
    assert stored_event_at == old_ts
    # evaluation_due_at = event_at + 7d (Discord target_stop default).
    stored_due_at = row.evaluation_due_at
    if stored_due_at.tzinfo is None:
        stored_due_at = stored_due_at.replace(tzinfo=timezone.utc)
    assert stored_due_at == old_ts + timedelta(days=7)


# ---------------------------------------------------------------------------
# Loop / JobRegistry integration
# ---------------------------------------------------------------------------


def test_metadata_shape():
    """``predictions_backfill_discord_metadata`` matches the spec
    contract: manual-only (cron=None), ingest, not long-running.
    """
    meta = predictions_backfill_discord_metadata()
    assert meta.name == "predictions_backfill_discord"
    assert meta.source_kind == "ingest"
    assert meta.long_running is False
    assert meta.schedule_cron is None
    assert "manual" in meta.schedule_human.lower()


def test_loop_is_disabled_by_default():
    """The loop ships with ``enabled=False`` so the scheduler's
    auto-tick path skips it; the manual ``fire_now`` path is the
    only invocation surface (see module docstring).
    """
    loop = PredictionsBackfillDiscordLoop()
    assert loop.enabled is False
    assert loop.name == "predictions_backfill_discord"


def test_loop_rejects_non_positive_lookback():
    with pytest.raises(ValueError, match="lookback_days"):
        PredictionsBackfillDiscordLoop(lookback_days=0)
    with pytest.raises(ValueError, match="lookback_days"):
        PredictionsBackfillDiscordLoop(lookback_days=-1)


@pytest.mark.asyncio
async def test_loop_tick_runs_backfill_end_to_end(sync_session):
    """:meth:`PredictionsBackfillDiscordLoop.tick` runs the backfill
    body against the injected session-factory + page-fetcher and
    returns the summary dict (Spec A commit #7 contract — the
    registry stores it on ``job_runs.output_summary``).
    """
    db, SessionLocal, _ = sync_session

    msgs = [
        _msg(
            msg_id=f"loop-{i}",
            ts=NOW - timedelta(hours=i + 1),
            content=f"BUY ${_TICKER_POOL[i]} target 200 stop 150",
        )
        for i in range(3)
    ]
    fetcher = _mk_stub_fetcher([msgs])

    loop = PredictionsBackfillDiscordLoop(
        session_factory=SessionLocal,
        lookback_days=14,
        page_fetcher=fetcher,
        now_fn=_now,
        # Pass creds directly so the loop doesn't try to consult
        # the real ~/.argosy/discord_creds.json in the test env.
        channel_id=CHANNEL_ID,
        bot_token=BOT_TOKEN,
    )
    result = await loop.tick()

    assert isinstance(result, dict)
    assert result["predictions_written"] == 3
    assert result["predictions_deduped"] == 0
    assert result["errors"] == []
    # Spec A NICE #7 side-channel — populated for the exception-path
    # fallback.
    assert loop.last_output_summary == result


@pytest.mark.asyncio
async def test_loop_fire_now_via_jobregistry_runs_backfill(sync_session):
    """Codex review focus — the manual-trigger path
    (:meth:`JobRegistry.fire_now`) reaches our tick body even though
    ``loop.enabled is False``. End-to-end integration test against a
    real :class:`JobRegistry` + :class:`RegisteredScheduler`.
    """
    db, SessionLocal, db_path = sync_session

    msgs = [
        _msg(
            msg_id=f"firenow-{i}",
            ts=NOW - timedelta(hours=i + 1),
            content=f"BUY ${_TICKER_POOL[i]} target 200 stop 150",
        )
        for i in range(2)
    ]
    fetcher = _mk_stub_fetcher([msgs])

    # Point the global async DB at our test sqlite so the
    # JobRegistry's audit-row writes land in the same file as the
    # synchronous predictions writes.
    from argosy.state import db as db_mod
    db_mod.init_engine(f"sqlite+aiosqlite:///{db_path}")

    from argosy.services.jobs.registered_scheduler import (
        RegisteredScheduler,
    )
    from argosy.services.jobs.registry import JobRegistry

    registry = JobRegistry()
    scheduler = RegisteredScheduler(registry=registry)
    registry.bind_scheduler(scheduler)

    loop = PredictionsBackfillDiscordLoop(
        session_factory=SessionLocal,
        lookback_days=14,
        page_fetcher=fetcher,
        now_fn=_now,
        channel_id=CHANNEL_ID,
        bot_token=BOT_TOKEN,
    )
    scheduler.register_loop(loop)
    registry.register(
        job=loop,
        metadata=predictions_backfill_discord_metadata(),
    )

    # Fire now — this is the manual path; should drive our tick body
    # even with ``enabled=False``.
    run_id = await registry.fire_now(
        "predictions_backfill_discord",
        triggered_by="test:fire_now",
    )
    assert run_id > 0

    # Verify the prediction was written via the manual path.
    count = db.scalar(
        sa.select(sa.func.count(Prediction.id)).where(
            Prediction.source == "discord"
        )
    )
    assert count == 2

    # Verify the audit row landed with the expected output_summary.
    from argosy.state.models import JobRun
    job_run = db.scalar(
        sa.select(JobRun).where(JobRun.id == run_id)
    )
    assert job_run is not None
    assert job_run.status == "ok"
    assert job_run.manual_trigger == 1
    assert job_run.triggered_by == "test:fire_now"
    summary = json.loads(job_run.output_summary)
    assert summary["predictions_written"] == 2


# ---------------------------------------------------------------------------
# Service-level: HTTP error short-circuits the run
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Service-level: attachment fetching (caption + .txt body combined)
# ---------------------------------------------------------------------------


def _msg_with_attachment(
    msg_id: str,
    ts: datetime,
    content: str,
    *,
    attachment_url: str,
    filename: str = "Alpha Report.txt",
    content_type: str = "text/plain",
    size: int = 256,
) -> dict[str, Any]:
    """Build a Discord message JSON object that carries one .txt
    attachment alongside a caption."""
    base = _msg(msg_id, ts, content)
    base["attachments"] = [
        {
            "id": f"att-{msg_id}",
            "filename": filename,
            "content_type": content_type,
            "size": size,
            "url": attachment_url,
            "proxy_url": attachment_url + "&proxy=1",
        }
    ]
    return base


@pytest.mark.asyncio
async def test_backfill_fetches_txt_attachment_and_parses_alpha_call(
    sync_session,
):
    """A historical Discord message with caption ``"Today's report"``
    (no parseable alpha call by itself) plus a ``.txt`` attachment
    containing ``BUY $NVDA target $180 stop $135`` → backfill must
    HTTPS-GET the attachment, combine caption+body, and write a
    prediction.
    """
    db, _, _ = sync_session

    attachment_url = (
        "https://cdn.discordapp.com/attachments/1/2/report.txt?"
        "ex=abc&is=def&hm=signature"
    )
    attachment_body = (
        "Alpha Report 5/29/2026\n"
        "BUY $NVDA target $180 stop $135\n"
        "Strong guidance, record revenue."
    )

    msgs = [
        _msg_with_attachment(
            msg_id="attmsg-001",
            ts=NOW - timedelta(hours=2),
            content="Today's report",  # caption alone has no alpha call
            attachment_url=attachment_url,
            size=len(attachment_body.encode("utf-8")),
        ),
    ]
    fetcher = _mk_stub_fetcher([msgs])

    def handler(request: httpx.Request) -> httpx.Response:
        # Confirm signed-URL contract: bot token NEVER reaches the CDN
        # (either no Authorization header, or our explicit empty scrub).
        auth = request.headers.get("Authorization")
        assert auth in (None, "")
        body_bytes = attachment_body.encode("utf-8")
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(body_bytes)),
            },
            content=body_bytes,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as http_client:
        summary = await backfill_discord_predictions(
            db,
            lookback_days=14,
            channel_id=CHANNEL_ID,
            bot_token=BOT_TOKEN,
            page_fetcher=fetcher,
            now=_now,
            attachment_http_client=http_client,
        )

    assert summary.predictions_written == 1
    assert summary.messages_unparseable == 0
    assert summary.errors == []

    row = db.scalar(sa.select(Prediction).where(Prediction.ticker == "NVDA"))
    assert row is not None
    assert row.direction == "long"


@pytest.mark.asyncio
async def test_backfill_skips_messages_with_only_image_attachments(
    sync_session,
):
    """Image-only attachment + caption with no alpha call → message
    counted as unparseable; NO HTTP call made (image filtered before
    fetch). Defensive: confirms a 5 MiB JPEG can't accidentally be
    pulled."""
    db, _, _ = sync_session

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(str(request.url))
        return httpx.Response(200, content=b"never")

    msg = _msg("img-msg-1", NOW - timedelta(hours=1), "just a picture")
    msg["attachments"] = [
        {
            "id": "img-1",
            "filename": "chart.png",
            "content_type": "image/png",
            "size": 500_000,
            "url": "https://cdn.discordapp.com/img.png?sig=1",
        }
    ]
    fetcher = _mk_stub_fetcher([[msg]])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as http_client:
        summary = await backfill_discord_predictions(
            db,
            lookback_days=14,
            channel_id=CHANNEL_ID,
            bot_token=BOT_TOKEN,
            page_fetcher=fetcher,
            now=_now,
            attachment_http_client=http_client,
        )

    assert calls == []
    assert summary.predictions_written == 0
    assert summary.messages_unparseable == 1


# ---------------------------------------------------------------------------
# Service-level: HTTP error short-circuits the run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_mid_run_short_circuits_with_partial_progress(
    sync_session,
):
    """If page 2 fails, page 1's successful writes survive in the
    summary + DB; the run terminates with an ``errors`` entry.
    """
    db, _, _ = sync_session

    page1 = [
        _msg(
            msg_id=f"good-{i}",
            ts=NOW - timedelta(hours=i + 1),
            content=f"BUY ${_TICKER_POOL[i]} target 200 stop 150",
        )
        for i in range(5)
    ]

    call_count = {"n": 0}

    async def fetcher(channel_id, before_id, bot_token):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return page1
        # Page 2 raises a transport error.
        raise httpx.ConnectError("simulated network blip")

    summary = await backfill_discord_predictions(
        db,
        lookback_days=14,
        channel_id=CHANNEL_ID,
        bot_token=BOT_TOKEN,
        page_fetcher=fetcher,
        now=_now,
    )

    # Page 1 succeeded → 5 predictions persisted; page 2 erred out.
    assert summary.predictions_written == 5
    assert summary.pages_fetched == 1
    assert summary.errors  # non-empty
    assert any("transport_error" in e for e in summary.errors)

    count = db.scalar(
        sa.select(sa.func.count(Prediction.id)).where(
            Prediction.source == "discord"
        )
    )
    assert count == 5
