"""Entry-backfill re-evaluation path (migration 0081 + evaluator v2 methods).

Covers:

* :func:`argosy.services.predictions.evaluator.find_reevaluation_candidates`
  — picks only due, unparseable-under-v1, not-yet-backfilled fixed-lookahead
  predictions; supports the ``source`` filter; includes archived rows.
* :func:`argosy.services.predictions.evaluator.reevaluate_prediction`
  — backfills the entry from the last close at/before ``event_at``,
  scores under ``<base>_entry_backfilled``, leaves the v1 outcome row
  untouched (insert-only supersession), and is idempotent.
* :func:`argosy.services.predictions.evaluator.run_reevaluation_batch`
  — batch counters + adapter-error isolation.
* Migration 0081 — the two v2 registry rows exist at alembic head with
  ``family='fixed_lookahead'`` + ``method_version=2`` (what the
  source_reliability view's dedup keys off).

Run:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_predictions_reevaluator.py -v
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from argosy.services.predictions.evaluator import (
    Bar,
    ENTRY_BACKFILL_SUFFIX,
    EvaluatorAdapterError,
    evaluate_prediction,
    find_reevaluation_candidates,
    reevaluate_prediction,
    run_reevaluation_batch,
)
from argosy.state.models import Prediction, PredictionOutcome


# ---------------------------------------------------------------------------
# Fixtures — same alembic-head-per-test idiom as test_predictions_evaluator
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path, monkeypatch) -> "tuple[Session, sessionmaker]":
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        conn.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) "
                "VALUES ('ariel', 'free', '2026-01-01 00:00:00+00:00')"
            )
        )

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session, factory
    finally:
        session.close()
        engine.dispose()


_INSERT_COUNTER = [0]

_EVENT_AT = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)


def _insert_prediction(
    session: Session,
    *,
    source: str = "discord_alpha_report",
    ticker: str | None = "NVDA",
    direction: str = "long",
    entry_price: float | None = None,
    timeframe_days: int = 30,
    event_at: datetime = _EVENT_AT,
    evaluation_method: str = "fixed_lookahead_30d",
    archived: int = 0,
) -> Prediction:
    _INSERT_COUNTER[0] += 1
    row = Prediction(
        user_id="ariel",
        source=source,
        source_ref="{}",
        ticker=ticker,
        direction=direction,
        entry_price=(
            Decimal(str(entry_price)) if entry_price is not None else None
        ),
        timeframe_days=timeframe_days,
        message_id=f"v1|predictions|{source}|{_INSERT_COUNTER[0]}",
        event_at=event_at,
        evaluation_due_at=event_at + timedelta(days=timeframe_days),
        evaluation_method=evaluation_method,
        archived=archived,
    )
    session.add(row)
    session.flush()
    return row


def _bars(*specs: tuple[str, float, float, float, float]) -> list[Bar]:
    return [
        Bar(
            bar_date=date.fromisoformat(d),
            open=o,
            high=h,
            low=lo,
            close=c,
        )
        for (d, o, h, lo, c) in specs
    ]


def _fetcher_returning(bars: list[Bar]):
    def _fetch(_ticker: str, _start: date, _end: date) -> list[Bar]:
        return list(bars)

    return _fetch


def _seed_v1_unparseable(session: Session, prediction: Prediction) -> None:
    """Score the prediction under its v1 method with an empty-bars
    fetcher → the historical ``unparseable`` outcome row."""
    evaluate_prediction(
        session, prediction, price_fetcher=_fetcher_returning([])
    )
    session.flush()


#: Bars spanning the event date (2026-05-01, a Friday) + the window.
#: Entry backfill must pick the 05-01 close (100.0), NOT the 04-28 one.
_FULL_BARS = _bars(
    ("2026-04-28", 95.0, 96.0, 94.0, 95.5),
    ("2026-05-01", 99.0, 101.0, 98.0, 100.0),
    ("2026-05-04", 101.0, 103.0, 100.0, 102.0),
    ("2026-05-29", 111.0, 113.0, 110.0, 112.0),
)


# ---------------------------------------------------------------------------
# Migration 0081 — registry seeds
# ---------------------------------------------------------------------------


def test_v2_methods_registered_at_head(sync_session) -> None:
    session, _ = sync_session
    rows = session.execute(
        sa.text(
            "SELECT method_name, family, method_version, is_active "
            "FROM evaluation_method_registry "
            "WHERE method_name LIKE '%entry_backfilled' "
            "ORDER BY method_name"
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        ("fixed_lookahead_180d_entry_backfilled", "fixed_lookahead", 2, 1),
        ("fixed_lookahead_30d_entry_backfilled", "fixed_lookahead", 2, 1),
        ("fixed_lookahead_7d_entry_backfilled", "fixed_lookahead", 2, 1),
    ]


# ---------------------------------------------------------------------------
# reevaluate_prediction
# ---------------------------------------------------------------------------


def test_backfilled_entry_scores_and_supersedes(sync_session) -> None:
    session, _ = sync_session
    p = _insert_prediction(session)  # long, entry NULL, 30d
    _seed_v1_unparseable(session, p)

    outcome = reevaluate_prediction(
        session, p, price_fetcher=_fetcher_returning(_FULL_BARS)
    )
    session.flush()

    # Entry = event-day close 100.0; exit = window-end close 112.0.
    assert outcome.evaluation_method == "fixed_lookahead_30d_entry_backfilled"
    assert outcome.outcome_kind == "hit_target"  # +12% >= 10% threshold
    assert float(outcome.entry_price_used) == pytest.approx(100.0)
    assert float(outcome.exit_price_used) == pytest.approx(112.0)
    assert float(outcome.pnl_pct) == pytest.approx(0.12)
    assert "entry backfilled from close(2026-05-01)" in outcome.notes

    # Insert-only supersession: BOTH rows exist; v1 untouched.
    all_rows = (
        session.execute(
            sa.select(PredictionOutcome).where(
                PredictionOutcome.prediction_id == p.id
            )
        )
        .scalars()
        .all()
    )
    kinds = {r.evaluation_method: r.outcome_kind for r in all_rows}
    assert kinds == {
        "fixed_lookahead_30d": "unparseable",
        "fixed_lookahead_30d_entry_backfilled": "hit_target",
    }


def test_entry_falls_back_to_last_close_before_event(sync_session) -> None:
    """event_at on a non-trading day → entry = last prior close."""
    session, _ = sync_session
    # 2026-05-02 is a Saturday; only the 04-28 + 05-01 bars precede it.
    p = _insert_prediction(
        session,
        event_at=datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc),
    )
    _seed_v1_unparseable(session, p)

    outcome = reevaluate_prediction(
        session, p, price_fetcher=_fetcher_returning(_FULL_BARS)
    )
    assert float(outcome.entry_price_used) == pytest.approx(100.0)
    assert "close(2026-05-01)" in outcome.notes


def test_short_direction_sign_flip(sync_session) -> None:
    session, _ = sync_session
    p = _insert_prediction(session, direction="short")
    _seed_v1_unparseable(session, p)

    outcome = reevaluate_prediction(
        session, p, price_fetcher=_fetcher_returning(_FULL_BARS)
    )
    # Price rose 12% against the short → signed pnl -12% → hit_stop.
    assert outcome.outcome_kind == "hit_stop"
    assert float(outcome.pnl_pct) == pytest.approx(-0.12)


def test_writer_entry_takes_precedence_over_backfill(sync_session) -> None:
    session, _ = sync_session
    p = _insert_prediction(session, entry_price=80.0)
    _seed_v1_unparseable(session, p)  # v1 failed on no-bars, not entry

    outcome = reevaluate_prediction(
        session, p, price_fetcher=_fetcher_returning(_FULL_BARS)
    )
    assert float(outcome.entry_price_used) == pytest.approx(80.0)
    assert outcome.notes == "entry from writer snapshot"
    assert float(outcome.pnl_pct) == pytest.approx((112.0 - 80.0) / 80.0)


def test_no_entry_bar_stays_unparseable(sync_session) -> None:
    session, _ = sync_session
    p = _insert_prediction(session)
    _seed_v1_unparseable(session, p)

    # Bars exist only INSIDE the window — nothing at/before event_at.
    window_only = _bars(("2026-05-04", 101.0, 103.0, 100.0, 102.0))
    outcome = reevaluate_prediction(
        session, p, price_fetcher=_fetcher_returning(window_only)
    )
    assert outcome.outcome_kind == "unparseable"
    assert "no entry bar" in outcome.notes


def test_reevaluate_is_idempotent(sync_session) -> None:
    session, _ = sync_session
    p = _insert_prediction(session)
    _seed_v1_unparseable(session, p)

    first = reevaluate_prediction(
        session, p, price_fetcher=_fetcher_returning(_FULL_BARS)
    )
    session.flush()
    second = reevaluate_prediction(
        session,
        p,
        # Different bars — must NOT be consulted on the idempotent path.
        price_fetcher=_fetcher_returning([]),
    )
    assert second.id == first.id
    assert second.outcome_kind == "hit_target"


# ---------------------------------------------------------------------------
# find_reevaluation_candidates
# ---------------------------------------------------------------------------


def test_candidate_selection(sync_session) -> None:
    session, _ = sync_session
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)

    # 1. v1-unparseable + due → candidate.
    cand = _insert_prediction(session)
    _seed_v1_unparseable(session, cand)

    # 2. v1 scored cleanly → NOT a candidate (verdict stands).
    scored = _insert_prediction(session, entry_price=100.0)
    evaluate_prediction(
        session, scored, price_fetcher=_fetcher_returning(_FULL_BARS)
    )

    # 3. Not due yet → NOT a candidate.
    _insert_prediction(
        session, event_at=now - timedelta(days=2), timeframe_days=30
    )

    # 4. target_stop method → NOT a candidate (backfill unsupported).
    ts = _insert_prediction(
        session, evaluation_method="target_stop"
    )
    evaluate_prediction(
        session, ts, price_fetcher=_fetcher_returning([])
    )

    # 5. Already backfilled → NOT a candidate.
    done = _insert_prediction(session)
    _seed_v1_unparseable(session, done)
    reevaluate_prediction(
        session, done, price_fetcher=_fetcher_returning(_FULL_BARS)
    )

    # 6. Archived but v1-unparseable → STILL a candidate.
    arch = _insert_prediction(session, archived=1)
    _seed_v1_unparseable(session, arch)

    # 7. Different source, filtered out by the source arg.
    other = _insert_prediction(session, source="internal_state_observer")
    _seed_v1_unparseable(session, other)

    session.flush()

    ids = {
        p.id
        for p in find_reevaluation_candidates(
            session, now=now, source="discord_alpha_report"
        )
    }
    assert ids == {cand.id, arch.id}

    # Without the source filter, the other-source row joins in.
    ids_all = {p.id for p in find_reevaluation_candidates(session, now=now)}
    assert ids_all == {cand.id, arch.id, other.id}


# ---------------------------------------------------------------------------
# run_reevaluation_batch
# ---------------------------------------------------------------------------


def test_batch_counters_and_adapter_error_isolation(sync_session) -> None:
    session, _ = sync_session
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)

    ok = _insert_prediction(session, ticker="NVDA")
    _seed_v1_unparseable(session, ok)
    bad = _insert_prediction(session, ticker="FAIL")
    _seed_v1_unparseable(session, bad)
    still = _insert_prediction(session, ticker="EMPTY")
    _seed_v1_unparseable(session, still)

    def _fetch(ticker: str, _start: date, _end: date) -> list[Bar]:
        if ticker == "FAIL":
            raise EvaluatorAdapterError("transient")
        if ticker == "EMPTY":
            return []
        return list(_FULL_BARS)

    summary = run_reevaluation_batch(
        session, now=now, price_fetcher=_fetch
    )
    assert summary.candidates == 3
    assert summary.reevaluated == 2
    assert summary.adapter_errors == 1
    assert summary.still_unparseable == 1
    assert summary.by_kind == {"hit_target": 1, "unparseable": 1}

    # The FAIL row got NO v2 outcome → retried on the next batch.
    retry = find_reevaluation_candidates(session, now=now)
    assert [p.id for p in retry] == [bad.id]

    # An EMPTY-bars row DID get a v2 unparseable row → settled, not
    # re-picked forever.
    empty_v2 = (
        session.execute(
            sa.select(PredictionOutcome).where(
                PredictionOutcome.prediction_id == still.id,
                PredictionOutcome.evaluation_method
                == "fixed_lookahead_30d" + ENTRY_BACKFILL_SUFFIX,
            )
        )
        .scalars()
        .one()
    )
    assert empty_v2.outcome_kind == "unparseable"
