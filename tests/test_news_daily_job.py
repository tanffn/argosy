"""``NewsDailyJob`` tests — Sprint A commit #7.

Spec: ``docs/superpowers/specs/2026-05-29-jobs-registry-design.md`` commit #7.

Surface:

* Happy path: both stages succeed → ``tick()`` returns a dict with the
  ``counts.ingested_*`` + ``counts.analyzed_*`` keys and
  ``stages={ingest: ok, analyze: ok}``.
* Stage 1 success, Stage 2 fails: ``tick()`` re-raises, but
  ``self.last_output_summary`` is populated (set in ``finally``) with
  ``stages={ingest: ok, analyze: error}`` plus ``stage_errors.analyze``.
  The :class:`RegisteredScheduler` adapter reads this attribute on the
  exception path so the audit row records partial progress.
* Same-session contract: ``tick()`` passes the SAME ``Session`` object to
  the ingest stub AND the analyst stub (no per-stage session churn).
* Schedule defaults: the constructor's default ``LoopSchedule`` matches
  ``cadences.news_daily`` (cron + tz).

Test command::

    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_news_daily_job.py -v
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.jobs.news_daily import (
    HoldingsTickers,
    NewsDailyJob,
    _build_summary,
    news_daily_metadata,
    resolve_holdings_split,
)
from argosy.services.news_analyst_runner import AnalysisRunResult
from argosy.services.news_ingest import NewsIngestResult
from argosy.state.models import Base, NewsSignal


# ---------------------------------------------------------------------------
# Fixtures — in-process sync session factory
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(tmp_path):
    """File-backed SQLite sessionmaker mirroring the existing
    test_news_ingest pattern."""
    db_path = tmp_path / "news_daily_test.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield SF
    finally:
        engine.dispose()


def _ok_ingest_result() -> NewsIngestResult:
    return NewsIngestResult(
        fetched=5,
        persisted=3,
        duplicates=2,
        by_source={"rss": 3, "macro_feed": 2, "discord": 0},
    )


def _ok_analyst_result() -> AnalysisRunResult:
    return AnalysisRunResult(fetched=3, analyzed=3, skipped=0, batches=1)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_tick_happy_path_returns_dict(session_factory) -> None:
    """Both stages succeed → tick returns the spec-shaped summary dict."""
    ingest_calls: list[object] = []
    analyst_calls: list[object] = []

    def fake_ingest(session, **kwargs):
        ingest_calls.append(session)
        return _ok_ingest_result()

    def fake_analyst(session, **kwargs):
        analyst_calls.append(session)
        return _ok_analyst_result()

    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=fake_ingest,
        analyst_fn=fake_analyst,
        agent_factory=lambda: MagicMock(),  # never invoked by the stub
    )

    result = asyncio.run(job.tick())

    assert isinstance(result, dict)
    assert result["stages"] == {"ingest": "ok", "analyze": "ok"}
    assert result["stage_errors"] == {}
    assert result["counts"]["ingested_fetched"] == 5
    assert result["counts"]["ingested_persisted"] == 3
    assert result["counts"]["ingested_duplicates"] == 2
    assert result["counts"]["analyzed"] == 3
    assert result["counts"]["analyzed_batches"] == 1

    # `last_output_summary` mirrors the return value.
    assert job.last_output_summary == result

    assert len(ingest_calls) == 1
    assert len(analyst_calls) == 1


def test_tick_stages_share_one_session(session_factory) -> None:
    """The same Session object is threaded through Stage 1 + Stage 2.

    The session-mock pattern verifies session identity: if a future
    refactor were to open a fresh session per stage, Stage 2 might not
    see Stage 1's rows (depending on autocommit) and partial-progress
    semantics would silently break.
    """
    sessions_seen: list[object] = []

    def fake_ingest(session, **kwargs):
        sessions_seen.append(session)
        return _ok_ingest_result()

    def fake_analyst(session, **kwargs):
        sessions_seen.append(session)
        return _ok_analyst_result()

    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=fake_ingest,
        analyst_fn=fake_analyst,
        agent_factory=lambda: MagicMock(),
    )

    asyncio.run(job.tick())

    assert len(sessions_seen) == 2
    assert sessions_seen[0] is sessions_seen[1], (
        "Stage 1 and Stage 2 must share one Session — got "
        f"id1={id(sessions_seen[0])} id2={id(sessions_seen[1])}"
    )


# ---------------------------------------------------------------------------
# Stage 2 failure — partial-progress capture
# ---------------------------------------------------------------------------


def test_tick_stage2_failure_populates_last_output_summary(
    session_factory,
) -> None:
    """Stage 1 ok + Stage 2 raise → tick re-raises but the attribute is set.

    Pin: the adapter reads ``self.last_output_summary`` on the exception
    path. The dict must:

    * report ``stages.ingest='ok'`` and ``stages.analyze='error'``
    * contain Stage 1's full ``counts.ingested_*`` block
    * contain ``stage_errors.analyze`` with the exception string
    """

    def fake_ingest(session, **kwargs):
        return _ok_ingest_result()

    def fake_analyst_boom(session, **kwargs):
        raise RuntimeError("analyst exploded")

    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=fake_ingest,
        analyst_fn=fake_analyst_boom,
        agent_factory=lambda: MagicMock(),
    )

    with pytest.raises(RuntimeError, match="analyst exploded"):
        asyncio.run(job.tick())

    summary = job.last_output_summary
    assert summary is not None
    assert summary["stages"]["ingest"] == "ok"
    assert summary["stages"]["analyze"] == "error"
    assert "analyst exploded" in summary["stage_errors"]["analyze"]
    # Stage 1's counts are still surfaced.
    assert summary["counts"]["ingested_fetched"] == 5
    assert summary["counts"]["ingested_persisted"] == 3
    # Stage 2's counts default to zero.
    assert summary["counts"]["analyzed"] == 0
    assert summary["counts"]["analyzed_batches"] == 0


def test_tick_stage1_failure_short_circuits(session_factory) -> None:
    """Stage 1 failure → Stage 2 is NEVER called; summary reports it."""
    analyst_called = False

    def fake_ingest_boom(session, **kwargs):
        raise RuntimeError("ingest exploded")

    def fake_analyst(session, **kwargs):
        nonlocal analyst_called
        analyst_called = True
        return _ok_analyst_result()

    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=fake_ingest_boom,
        analyst_fn=fake_analyst,
        agent_factory=lambda: MagicMock(),
    )

    with pytest.raises(RuntimeError, match="ingest exploded"):
        asyncio.run(job.tick())

    assert analyst_called is False
    summary = job.last_output_summary
    assert summary is not None
    assert summary["stages"] == {"ingest": "error", "analyze": "pending"}
    assert "ingest exploded" in summary["stage_errors"]["ingest"]


# ---------------------------------------------------------------------------
# Metadata + schedule
# ---------------------------------------------------------------------------


def test_news_daily_metadata_shape() -> None:
    meta = news_daily_metadata()
    assert meta.name == "news_daily"
    assert meta.schedule_cron == "0 17 * * *"
    assert meta.source_kind == "ingest"
    assert meta.long_running is False


def test_default_schedule_is_17_il_local() -> None:
    """The constructor's default LoopSchedule matches cadences.news_daily."""
    job = NewsDailyJob()
    assert job.schedule.cron == "0 17 * * *"
    assert job.schedule.timezone == "Asia/Jerusalem"


def test_agent_settings_exposes_news_daily_cadence() -> None:
    """`cadences.news_daily` defaults to enabled + 17:00 IL-local."""
    from argosy.agent_settings import AgentSettings

    settings = AgentSettings()
    cad = settings.cadences.news_daily
    assert cad.enabled is True
    assert cad.cron == "0 17 * * *"
    assert cad.timezone == "Asia/Jerusalem"


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def test_build_summary_with_no_results() -> None:
    """`_build_summary` handles the both-stages-skipped case (defensive)."""
    summary = _build_summary(
        stage1_result=None,
        stage2_result=None,
        stage1_status="pending",
        stage2_status="pending",
        stage1_error=None,
        stage2_error=None,
    )
    assert summary["counts"]["ingested_fetched"] == 0
    assert summary["counts"]["analyzed"] == 0
    assert summary["stages"] == {"ingest": "pending", "analyze": "pending"}
    assert summary["stage_errors"] == {}


def test_last_output_summary_reset_between_ticks(session_factory) -> None:
    """Codex review #7 BLOCKER: a tick that fails BEFORE the try/finally
    must not let the prior tick's summary leak to the adapter.

    We simulate a session_factory that raises (e.g. DB file moved /
    permission denied). The attribute MUST be reset to None before
    the work begins, so the adapter sees a clean slate rather than
    yesterday's success summary.
    """
    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=lambda s, **kw: _ok_ingest_result(),
        analyst_fn=lambda s, **kw: _ok_analyst_result(),
        agent_factory=lambda: MagicMock(),
    )

    # First tick succeeds — populate the attribute.
    asyncio.run(job.tick())
    assert job.last_output_summary is not None
    assert job.last_output_summary["stages"]["ingest"] == "ok"

    # Second tick: replace session_factory with one that raises BEFORE
    # the try/finally inside _run_stages_sync can populate the summary.
    def boom_factory():
        raise RuntimeError("db file gone")

    job._session_factory = boom_factory

    with pytest.raises(RuntimeError, match="db file gone"):
        asyncio.run(job.tick())

    # The prior success summary must NOT have leaked — tick() resets the
    # attribute at entry.
    assert job.last_output_summary is None


def test_stage2_classified_error_when_agent_init_fails(session_factory) -> None:
    """Codex review #7 BLOCKER: if NewsSignalAnalystAgent construction
    raises, the failure is a Stage 2 problem — not "still pending".

    Pin: agent_factory raising → stages.analyze == "error" and
    stage_errors.analyze captures the exception string.
    """
    def boom_agent_factory():
        raise RuntimeError("missing ANTHROPIC_API_KEY")

    analyst_called = False

    def fake_analyst(session, **kwargs):
        nonlocal analyst_called
        analyst_called = True
        return _ok_analyst_result()

    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=lambda s, **kw: _ok_ingest_result(),
        analyst_fn=fake_analyst,
        agent_factory=boom_agent_factory,
    )

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        asyncio.run(job.tick())

    assert analyst_called is False, (
        "analyst_fn must not be called when agent_factory raises"
    )
    summary = job.last_output_summary
    assert summary is not None
    assert summary["stages"]["ingest"] == "ok"
    assert summary["stages"]["analyze"] == "error", (
        "Agent construction failure must be classified as Stage 2 error, "
        f"got {summary['stages']!r}"
    )
    assert "ANTHROPIC_API_KEY" in summary["stage_errors"]["analyze"]


def test_default_session_factory_is_cached(monkeypatch, tmp_path) -> None:
    """Codex review #7 BLOCKER: the default factory must be cached to
    avoid building a new engine per tick.

    Pin: two calls with the same db_file return the SAME sessionmaker
    instance. This proves the cache is wired.
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    from argosy.services.jobs.news_daily import (
        _build_default_session_factory,
        _reset_default_session_factory_cache,
    )

    reload_settings()
    _reset_default_session_factory_cache()

    f1 = _build_default_session_factory()
    f2 = _build_default_session_factory()
    assert f1 is f2, "default session factory must be cached across ticks"


# ---------------------------------------------------------------------------
# RegisteredScheduler ↔ NewsDailyJob integration (codex review #7 IMPORTANT)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_news_daily_through_registered_scheduler_success(
    engine: None, session_factory,
) -> None:
    """End-to-end: RegisteredScheduler.fire_now → NewsDailyJob.tick →
    output_summary persisted to job_runs.

    Pins the return-value capture path: the tick's explicit ``dict``
    return MUST reach ``job_runs.output_summary`` (not via
    ``last_output_summary`` — the success branch should prefer the
    return value).
    """
    from sqlalchemy import select

    from argosy.services.jobs import (
        JobMetadata,
        JobRegistry,
        RegisteredScheduler,
    )
    from argosy.state import db as db_mod
    from argosy.state.models import JobRun

    class _DummySettings:
        pass

    registry = JobRegistry()
    scheduler = RegisteredScheduler(
        user_id="ariel", settings=_DummySettings(), registry=registry
    )
    registry.bind_scheduler(scheduler)

    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=lambda s, **kw: _ok_ingest_result(),
        analyst_fn=lambda s, **kw: _ok_analyst_result(),
        agent_factory=lambda: MagicMock(),
    )
    scheduler.register_loop(job)
    registry.register(job=job, metadata=news_daily_metadata())

    run_id = await registry.fire_now("news_daily", triggered_by="test")

    async with db_mod.get_session() as s:
        row = (
            await s.execute(select(JobRun).where(JobRun.id == run_id))
        ).scalar_one()
        assert row.status == "ok"
        assert row.output_summary is not None
        # Spec shape: counts.ingested_persisted + stages.ingest=ok must
        # be present in the persisted JSON blob.
        assert '"ingested_persisted": 3' in row.output_summary
        assert '"ingest": "ok"' in row.output_summary
        assert '"analyze": "ok"' in row.output_summary


@pytest.mark.asyncio
async def test_news_daily_through_registered_scheduler_stage2_failure(
    engine: None, session_factory,
) -> None:
    """End-to-end: Stage 2 raises → audit row is status='error' AND
    output_summary captures Stage 1's counts via the attribute fallback.

    Pins the exception-path adapter behavior added in commit #7.
    """
    from sqlalchemy import select

    from argosy.services.jobs import (
        JobMetadata,
        JobRegistry,
        RegisteredScheduler,
    )
    from argosy.state import db as db_mod
    from argosy.state.models import JobRun

    class _DummySettings:
        pass

    registry = JobRegistry()
    scheduler = RegisteredScheduler(
        user_id="ariel", settings=_DummySettings(), registry=registry
    )
    registry.bind_scheduler(scheduler)

    def boom(session, **kw):
        raise RuntimeError("analyst exploded")

    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=lambda s, **kw: _ok_ingest_result(),
        analyst_fn=boom,
        agent_factory=lambda: MagicMock(),
    )
    scheduler.register_loop(job)
    registry.register(job=job, metadata=news_daily_metadata())

    # fire_now propagates the tick exception (force=True path) AFTER the
    # adapter has already closed the audit row with status='error'. So
    # we expect the raise, then assert on the persisted row.
    with pytest.raises(RuntimeError, match="analyst exploded"):
        await registry.fire_now("news_daily", triggered_by="test")

    async with db_mod.get_session() as s:
        row = (
            await s.execute(
                select(JobRun)
                .where(JobRun.job_name == "news_daily")
                .order_by(JobRun.id.desc())
                .limit(1)
            )
        ).scalar_one()
        assert row.status == "error"
        assert "analyst exploded" in (row.error_message or "")
        # The adapter must have read self.last_output_summary on the
        # exception path — Stage 1's counts are preserved.
        assert row.output_summary is not None, (
            "output_summary must be populated even on exception path "
            "via last_output_summary fallback"
        )
        assert '"ingested_persisted": 3' in row.output_summary
        assert '"ingest": "ok"' in row.output_summary
        assert '"analyze": "error"' in row.output_summary


# ---------------------------------------------------------------------------
# Phase C1 — smart news intake: tick-time ticker resolution + Stage-2 triage
# ---------------------------------------------------------------------------


def _no_new_ingest_result() -> NewsIngestResult:
    """Stage 1 fetched items but every one deduped — nothing new."""
    return NewsIngestResult(
        fetched=2,
        persisted=0,
        duplicates=2,
        by_source={"rss": 0, "macro_feed": 2, "discord": 0},
    )


def _persist_fake_snapshot(session_factory) -> None:
    """Persist a snapshot with single stocks + an ETF + cash + an unknown."""
    from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot
    from argosy.services.portfolio_snapshot_store import persist_snapshot

    snap = PortfolioSnapshot(
        source_path="fake-test.tsv",
        positions=[
            PortfolioPosition(
                location="Schwab", asset_type="Individual Stocks",
                details="Stock, AI", symbol="NVDA", usd_value_k=100.0,
            ),
            PortfolioPosition(
                location="Schwab", asset_type="High-potential sleeve",
                details="Stock, quantum", symbol="RGTI", usd_value_k=10.0,
            ),
            PortfolioPosition(
                location="Leumi", asset_type="Core Equity",
                details="ETF", symbol="VOO", usd_value_k=50.0,
            ),
            PortfolioPosition(
                location="Leumi", asset_type="Cash",
                details="", symbol="-", usd_value_k=20.0,
            ),
            # Unknown instrument with no single-stock evidence → light
            # (fund/macro) treatment, never a per-name fetch.
            PortfolioPosition(
                location="Schwab", asset_type="Other",
                details="Mystery holding", symbol="ZZZT", usd_value_k=5.0,
            ),
        ],
    )
    session = session_factory()
    try:
        persist_snapshot(session, user_id="ariel", snapshot=snap)
    finally:
        session.close()


def _seed_unanalyzed_signal(session_factory, *, source_ref: str = "seed-1") -> None:
    session = session_factory()
    try:
        session.add(NewsSignal(
            source="rss",
            source_ref=source_ref,
            received_at=datetime.now(UTC),
            parsed_tickers='["NVDA"]',
            event_keywords="[]",
            sentiment="neutral",
            source_trust="medium",
            evidence_excerpt="seeded evidence",
            raw_text="seeded raw",
        ))
        session.commit()
    finally:
        session.close()


def test_resolve_holdings_split_excludes_etfs_and_cash(session_factory) -> None:
    """Snapshot → single stocks at full priority; ETFs/cash/unknowns light."""
    _persist_fake_snapshot(session_factory)
    session = session_factory()
    try:
        split = resolve_holdings_split(session, "ariel")
    finally:
        session.close()
    assert split.single_stocks == ("NVDA", "RGTI")
    assert "VOO" not in split.single_stocks
    assert set(split.funds) == {"VOO", "ZZZT"}


def test_resolve_holdings_split_empty_without_snapshot(session_factory) -> None:
    session = session_factory()
    try:
        split = resolve_holdings_split(session, "ariel")
    finally:
        session.close()
    assert split == HoldingsTickers()


def test_tickers_resolved_from_snapshot_at_tick_time(session_factory) -> None:
    """The per-name fetch list comes from the LATEST snapshot at tick time:
    held single stocks in, ETFs/funds excluded, whitelist extended, and the
    analyst's materiality context covers the whole book."""
    _persist_fake_snapshot(session_factory)

    ingest_kwargs: dict = {}
    analyst_kwargs: dict = {}

    def fake_ingest(session, **kwargs):
        ingest_kwargs.update(kwargs)
        return _ok_ingest_result()

    def fake_analyst(session, **kwargs):
        analyst_kwargs.update(kwargs)
        return _ok_analyst_result()

    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=fake_ingest,
        analyst_fn=fake_analyst,
        agent_factory=lambda: MagicMock(),
        price_move_fn=lambda ts: {},
    )
    result = asyncio.run(job.tick())

    assert ingest_kwargs["tickers"] == ["NVDA", "RGTI"]
    # Extractor whitelist extended with the actual book (moonshots parse).
    assert {"NVDA", "RGTI", "VOO", "ZZZT"} <= set(ingest_kwargs["known_tickers"])
    # Analyst materiality context = the whole book (stocks + funds).
    assert set(analyst_kwargs["user_holdings"]) == {"NVDA", "RGTI", "VOO", "ZZZT"}
    # Summary surfaces the resolution for the owner to verify live.
    assert result["tickers"]["single_stocks"] == ["NVDA", "RGTI"]
    assert result["tickers"]["funds_light_treatment"] == 2


def test_stage2_skipped_when_nothing_new(session_factory) -> None:
    """Zero new items + no volatility → no agent construction, no analyst
    call, quiet summary {'analyzed': 0, 'reason': 'no new signals'}."""
    analyst_called = False
    agent_constructed = False

    def fake_analyst(session, **kwargs):
        nonlocal analyst_called
        analyst_called = True
        return _ok_analyst_result()

    def agent_factory():
        nonlocal agent_constructed
        agent_constructed = True
        return MagicMock()

    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=lambda s, **kw: _no_new_ingest_result(),
        analyst_fn=fake_analyst,
        agent_factory=agent_factory,
        price_move_fn=lambda ts: {},
        tickers=["NVDA"],
    )
    result = asyncio.run(job.tick())

    assert analyst_called is False
    assert agent_constructed is False, (
        "quiet day must not even construct the analyst agent"
    )
    assert result["stages"] == {"ingest": "ok", "analyze": "skipped"}
    assert result["analyzed"] == 0
    assert result["reason"] == "no new signals"
    assert result["counts"]["analyzed"] == 0
    assert result["stage2_gate"]["fired"] is False


def test_stage2_fires_on_new_headline(session_factory) -> None:
    """New persisted signals → the analyst fires; the (network) price sweep
    is never consulted — cheap-first triage."""
    analyst_called = False
    sweep_called = False

    def fake_analyst(session, **kwargs):
        nonlocal analyst_called
        analyst_called = True
        return _ok_analyst_result()

    def fake_sweep(tickers):
        nonlocal sweep_called
        sweep_called = True
        return {}

    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=lambda s, **kw: _ok_ingest_result(),  # persisted=3
        analyst_fn=fake_analyst,
        agent_factory=lambda: MagicMock(),
        price_move_fn=fake_sweep,
        tickers=["NVDA"],
    )
    result = asyncio.run(job.tick())

    assert analyst_called is True
    assert sweep_called is False, "new signals already fire — skip the sweep"
    assert result["stage2_gate"] == {
        "fired": True,
        "reasons": ["new_signals"],
        "pending_unanalyzed": 0,
        "volatility_moves": {},
    }


def test_stage2_fires_on_volatility_trigger(session_factory) -> None:
    """No new headlines, but a held single stock moved beyond the threshold
    while unanalyzed signals are pending → the analyst fires."""
    _seed_unanalyzed_signal(session_factory)
    analyst_called = False

    def fake_analyst(session, **kwargs):
        nonlocal analyst_called
        analyst_called = True
        return _ok_analyst_result()

    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=lambda s, **kw: _no_new_ingest_result(),
        analyst_fn=fake_analyst,
        agent_factory=lambda: MagicMock(),
        price_move_fn=lambda ts: {"NVDA": -6.5, "RGTI": 1.0},
        volatility_move_pct=4.0,
        tickers=["NVDA", "RGTI"],
    )
    result = asyncio.run(job.tick())

    assert analyst_called is True
    gate = result["stage2_gate"]
    assert gate["fired"] is True
    assert gate["reasons"] == ["volatility_trigger"]
    # Only the beyond-threshold mover is reported; RGTI (+1.0%) is not.
    assert gate["volatility_moves"] == {"NVDA": -6.5}
    assert gate["pending_unanalyzed"] == 1


def test_volatility_below_threshold_stays_quiet(session_factory) -> None:
    """A sub-threshold move is NOT a trigger — quiet day."""
    _seed_unanalyzed_signal(session_factory)
    analyst_called = False

    def fake_analyst(session, **kwargs):
        nonlocal analyst_called
        analyst_called = True
        return _ok_analyst_result()

    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=lambda s, **kw: _no_new_ingest_result(),
        analyst_fn=fake_analyst,
        agent_factory=lambda: MagicMock(),
        price_move_fn=lambda ts: {"NVDA": 2.0},
        volatility_move_pct=4.0,
        tickers=["NVDA"],
    )
    result = asyncio.run(job.tick())

    assert analyst_called is False
    assert result["reason"] == "no new signals"
    assert result["stage2_gate"]["volatility_moves"] == {}


def test_volatility_trigger_without_pending_signals_stays_quiet(
    session_factory,
) -> None:
    """A big move with ZERO unanalyzed rows gives the analyst nothing to
    read — skip, and say why."""
    analyst_called = False

    def fake_analyst(session, **kwargs):
        nonlocal analyst_called
        analyst_called = True
        return _ok_analyst_result()

    job = NewsDailyJob(
        session_factory=session_factory,
        ingest_fn=lambda s, **kw: _no_new_ingest_result(),
        analyst_fn=fake_analyst,
        agent_factory=lambda: MagicMock(),
        price_move_fn=lambda ts: {"NVDA": -8.0},
        volatility_move_pct=4.0,
        tickers=["NVDA"],
    )
    result = asyncio.run(job.tick())

    assert analyst_called is False
    assert result["analyzed"] == 0
    assert "volatility trigger" in result["reason"]
    assert result["stage2_gate"]["volatility_moves"] == {"NVDA": -8.0}


def test_volatility_threshold_default_from_config() -> None:
    """The trigger threshold is a config field, not a magic number."""
    from argosy.config import get_settings

    assert get_settings().news_volatility_move_pct == 4.0


def test_macro_calendar_staleness_warning(caplog) -> None:
    """Within 30 days of the curated list's last entry → LOUD log warning;
    well before it → silent."""
    from argosy.services.macro_feed import get_upcoming_macro_events

    logger_name = "argosy.services.macro_feed"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        get_upcoming_macro_events(now=datetime(2026, 12, 1, tzinfo=UTC))
    assert any(
        "EXHAUSTED" in r.getMessage() for r in caplog.records
    ), "expected a staleness warning within 30 days of the last curated event"

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=logger_name):
        get_upcoming_macro_events(now=datetime(2026, 7, 1, tzinfo=UTC))
    assert not any("EXHAUSTED" in r.getMessage() for r in caplog.records)
