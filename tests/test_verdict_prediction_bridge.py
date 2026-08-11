"""SEAM 1 (verdict recheck date) + SEAM 2 (verdict → graded prediction) tests.

SEAM 1: ``argosy.decisions.flow._derive_next_validation`` derivation + the
verdict registry persisting the derived ``next_validation``.

SEAM 2: ``argosy.services.predictions.writers.write_deep_decision_verdict_prediction``
emits one gradeable prediction per settled verdict, deduped on verdict_id, and
the LIVE evaluator scores it end-to-end over a seeded price series.

Run:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_verdict_prediction_bridge.py -q --timeout=180
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.decisions.flow import (
    _DEFAULT_VERDICT_RECHECK_DAYS,
    _derive_next_validation,
)
from argosy.services.predictions.evaluator import Bar, run_evaluator_batch
from argosy.services.predictions.writers import (
    DEEP_DECISION_VERDICT_SOURCE,
    emit_verdict_prediction_best_effort,
    write_deep_decision_verdict_prediction,
)
from argosy.state.models import Prediction, PredictionOutcome, User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(alembic_engine_at_head):
    """Sync sessionmaker at alembic head with FK enforcement + seeded user."""
    with alembic_engine_at_head.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
    factory = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = factory()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    s.close()
    return factory


def _bars(*specs: tuple[str, float, float, float, float]) -> list[Bar]:
    return [
        Bar(bar_date=date.fromisoformat(d), open=o, high=h, low=lo, close=c)
        for (d, o, h, lo, c) in specs
    ]


# ---------------------------------------------------------------------------
# SEAM 1 — next_validation derivation
# ---------------------------------------------------------------------------


def test_next_validation_from_soonest_future_dated_event():
    today = date(2026, 8, 11)
    triggers = [
        {"kind": "dated_event", "date": "2026-12-01", "label": "FY guide"},
        {"kind": "dated_event", "date": "2026-09-15", "label": "Q3 print"},
        {"kind": "price_below", "price": 100.0},
    ]
    assert _derive_next_validation(triggers, now=today) == date(2026, 9, 15)


def test_next_validation_default_horizon_when_no_dated_event():
    today = date(2026, 8, 11)
    triggers = [{"kind": "price_below", "price": 100.0}]
    assert _derive_next_validation(triggers, now=today) == today + timedelta(
        days=_DEFAULT_VERDICT_RECHECK_DAYS
    )
    # No triggers at all → still the default horizon.
    assert _derive_next_validation([], now=today) == today + timedelta(
        days=_DEFAULT_VERDICT_RECHECK_DAYS
    )


def test_next_validation_skips_past_dated_events():
    today = date(2026, 8, 11)
    triggers = [{"kind": "dated_event", "date": "2026-01-01"}]
    # Only a past dated event → fall through to the default horizon.
    assert _derive_next_validation(triggers, now=today) == today + timedelta(
        days=_DEFAULT_VERDICT_RECHECK_DAYS
    )


def test_next_validation_same_day_event_is_not_future():
    today = date(2026, 8, 11)
    # A same-day (already-due) dated event is NOT a future recheck → skipped.
    same_day = [{"kind": "dated_event", "date": "2026-08-11"}]
    assert _derive_next_validation(same_day, now=today) == today + timedelta(
        days=_DEFAULT_VERDICT_RECHECK_DAYS
    )
    # ...but a later trigger alongside the same-day one is still picked.
    mixed = [
        {"kind": "dated_event", "date": "2026-08-11"},
        {"kind": "dated_event", "date": "2026-08-20"},
    ]
    assert _derive_next_validation(mixed, now=today) == date(2026, 8, 20)


def test_next_validation_honors_explicit_trader_value():
    today = date(2026, 8, 11)
    triggers = [{"kind": "dated_event", "date": "2026-09-15"}]
    # Explicit str wins over the trigger.
    assert _derive_next_validation(
        triggers, explicit="2026-10-20", now=today
    ) == date(2026, 10, 20)
    # Explicit date object honored too.
    assert _derive_next_validation(
        triggers, explicit=date(2026, 11, 1), now=today
    ) == date(2026, 11, 1)


def test_next_validation_persists_through_write_verdict(session_factory):
    from argosy.services.verdict_registry import get_settled_verdict, write_verdict

    s = session_factory()
    try:
        recheck = _derive_next_validation(
            [{"kind": "dated_event", "date": "2026-09-15"}],
            now=date(2026, 8, 11),
        )
        write_verdict(
            s,
            user_id="ariel",
            subject="NVDA",
            verdict="HOLD",
            conviction="HIGH",
            revisit_triggers=[{"kind": "dated_event", "date": "2026-09-15"}],
            next_validation=recheck,
            source_decision_run_id=None,
        )
        s.commit()
        got = get_settled_verdict(s, user_id="ariel", subject="NVDA")
        assert got is not None
        assert got.next_validation == date(2026, 9, 15)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# SEAM 1 / SEAM 2 — no orphan prediction when the settle transaction rolls back
# ---------------------------------------------------------------------------


def _make_flow():
    """A DecisionFlow shell with only the attrs _record_settled_verdict uses."""
    from types import SimpleNamespace

    from argosy.decisions.flow import DecisionFlow

    flow = object.__new__(DecisionFlow)
    flow.user_id = "ariel"
    flow.config = SimpleNamespace(skip_persistence=False)
    return flow


def _patch_settle_deps(monkeypatch, *, retract_raises: bool):
    """Stub the settle path's collaborators; return the captured emit calls."""
    import asyncio

    from types import SimpleNamespace

    import argosy.decisions.flow as flow_mod
    import argosy.decisions.retract_on_reversal as retract_mod
    import argosy.services.predictions.writers as writers_mod
    import argosy.services.spine.fleet_recording as spine_mod
    import argosy.services.verdict_registry as reg_mod

    # get_engine → an in-memory sqlite URL (write_verdict is stubbed, so no
    # schema is needed; the method still builds a real sync engine/session).
    monkeypatch.setattr(
        flow_mod.db_mod,
        "get_engine",
        lambda: SimpleNamespace(url="sqlite+aiosqlite:///:memory:"),
    )
    monkeypatch.setattr(
        reg_mod, "write_verdict", lambda *a, **k: SimpleNamespace(id=999)
    )

    def _retract(*a, **k):
        if retract_raises:
            raise RuntimeError("boom: retraction failed → rollback")

    monkeypatch.setattr(
        retract_mod, "retract_contradictory_open_proposals", _retract
    )
    monkeypatch.setattr(
        spine_mod, "record_fleet_decision_best_effort", lambda **k: None
    )

    emit_calls: list[dict] = []
    monkeypatch.setattr(
        writers_mod,
        "emit_verdict_prediction_best_effort",
        lambda **k: emit_calls.append(k),
    )
    return asyncio, emit_calls


def test_no_orphan_prediction_when_settle_rolls_back(monkeypatch):
    # Retraction raises AFTER the verdict flush → the transaction never commits.
    asyncio, emit_calls = _patch_settle_deps(monkeypatch, retract_raises=True)
    flow = _make_flow()
    # Must NOT raise out of settle (best-effort), and must NOT fire the bridge.
    asyncio.run(
        flow._record_settled_verdict(
            ticker="NVDA",
            verdict="SELL",
            conviction="HIGH",
            decision_run_id=1,
            reasoning_md="x",
            revisit_triggers=[],
        )
    )
    assert emit_calls == []  # no prediction emitted for a rolled-back verdict


def test_bridge_fires_after_successful_commit(monkeypatch):
    # Happy path: retraction succeeds, commit succeeds → bridge fires with the id.
    asyncio, emit_calls = _patch_settle_deps(monkeypatch, retract_raises=False)
    flow = _make_flow()
    asyncio.run(
        flow._record_settled_verdict(
            ticker="NVDA",
            verdict="SELL",
            conviction="HIGH",
            decision_run_id=1,
            reasoning_md="x",
            revisit_triggers=[],
            entry_price=145.0,
        )
    )
    assert len(emit_calls) == 1
    assert emit_calls[0]["verdict_id"] == 999
    assert emit_calls[0]["subject"] == "NVDA"


# ---------------------------------------------------------------------------
# SEAM 2 — writer shape + dedup + gradeability
# ---------------------------------------------------------------------------


def _count_preds(factory) -> int:
    s = factory()
    try:
        return s.query(Prediction).count()
    finally:
        s.close()


def test_sell_verdict_emits_target_stop_prediction(session_factory):
    s = session_factory()
    try:
        row = write_deep_decision_verdict_prediction(
            s,
            "ariel",
            verdict_id=42,
            subject="nvda",
            verdict="SELL",
            event_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            entry_price=145.0,
            revisit_triggers=[
                {"kind": "price_below", "price": 130.0},
                {"kind": "price_above", "price": 160.0},
            ],
        )
        s.commit()
        assert row is not None
        assert row.source == DEEP_DECISION_VERDICT_SOURCE
        assert row.direction == "short"
        assert row.evaluation_method == "target_stop"
        # short: target BELOW entry (130), stop ABOVE entry (160)
        assert float(row.target_price) == 130.0
        assert float(row.stop_price) == 160.0
        assert '"verdict_id": 42' in row.source_ref
    finally:
        s.close()


def test_emit_is_idempotent_on_verdict_id(session_factory):
    for _ in range(2):
        s = session_factory()
        try:
            write_deep_decision_verdict_prediction(
                s,
                "ariel",
                verdict_id=7,
                subject="AAPL",
                verdict="BUY",
                event_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                entry_price=100.0,
                revisit_triggers=[
                    {"kind": "price_above", "price": 120.0},
                    {"kind": "price_below", "price": 90.0},
                ],
            )
            s.commit()
        finally:
            s.close()
    assert _count_preds(session_factory) == 1


def test_hold_emits_directional_prediction(session_factory):
    s = session_factory()
    try:
        row = write_deep_decision_verdict_prediction(
            s,
            "ariel",
            verdict_id=9,
            subject="MSFT",
            verdict="HOLD",
            event_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            entry_price=100.0,
            revisit_triggers=[],
        )
        s.commit()
        assert row is not None
        assert row.direction == "neutral"
        # No numeric target for HOLD → direction-only fixed_lookahead grade.
        assert row.evaluation_method == "fixed_lookahead_30d"
        assert row.target_price is None
    finally:
        s.close()


def test_unknown_verdict_emits_nothing(session_factory):
    s = session_factory()
    try:
        row = write_deep_decision_verdict_prediction(
            s,
            "ariel",
            verdict_id=11,
            subject="TSLA",
            verdict="FROBNICATE",
            event_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        s.commit()
        assert row is None
    finally:
        s.close()
    assert _count_preds(session_factory) == 0


def test_null_entry_falls_back_to_fixed_lookahead(session_factory):
    # No entry price (market/long-hold BUY) → fixed_lookahead (backfill-eligible),
    # never a null-target target_stop unparseable row.
    s = session_factory()
    try:
        row = write_deep_decision_verdict_prediction(
            s,
            "ariel",
            verdict_id=13,
            subject="AMD",
            verdict="BUY",
            event_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            entry_price=None,
            revisit_triggers=[{"kind": "price_above", "price": 200.0}],
        )
        s.commit()
        assert row is not None
        assert row.evaluation_method == "fixed_lookahead_30d"
        assert row.entry_price is None
        assert row.target_price is None
    finally:
        s.close()


def test_best_effort_wrapper_uses_injected_factory(session_factory):
    pred = emit_verdict_prediction_best_effort(
        user_id="ariel",
        verdict_id=21,
        subject="GOOG",
        verdict="TRIM",
        event_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        entry_price=150.0,
        revisit_triggers=[
            {"kind": "price_below", "price": 140.0},
            {"kind": "price_above", "price": 165.0},
        ],
        session_factory=session_factory,
    )
    assert pred is not None
    assert _count_preds(session_factory) == 1


# ---------------------------------------------------------------------------
# SEAM 2 — end-to-end: the LIVE evaluator grades the verdict-sourced prediction
# ---------------------------------------------------------------------------


def _price_fetcher(bars: list[Bar]):
    def _fetch(ticker, start, end):  # noqa: ARG001
        return bars

    return _fetch


def test_live_evaluator_grades_target_stop_verdict_prediction(session_factory):
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    event_at = now - timedelta(days=31)  # due (window = 30d)

    s = session_factory()
    try:
        write_deep_decision_verdict_prediction(
            s,
            "ariel",
            verdict_id=100,
            subject="NVDA",
            verdict="SELL",
            event_at=event_at,
            entry_price=145.0,
            revisit_triggers=[
                {"kind": "price_below", "price": 130.0},
                {"kind": "price_above", "price": 160.0},
            ],
        )
        s.commit()
    finally:
        s.close()

    # Price fell to 129 mid-window → for a SHORT, target (130) is touched.
    mid = (event_at + timedelta(days=15)).date().isoformat()
    bars = _bars((mid, 140.0, 141.0, 129.0, 132.0))

    s = session_factory()
    try:
        summary = run_evaluator_batch(
            s, now=now, price_fetcher=_price_fetcher(bars)
        )
        s.commit()
        assert summary.evaluated == 1
        outcome = s.query(PredictionOutcome).one()
        assert outcome.outcome_kind == "hit_target"
    finally:
        s.close()


def test_live_evaluator_grades_directional_hold_prediction(session_factory):
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    event_at = now - timedelta(days=31)

    s = session_factory()
    try:
        write_deep_decision_verdict_prediction(
            s,
            "ariel",
            verdict_id=101,
            subject="MSFT",
            verdict="HOLD",
            event_at=event_at,
            entry_price=100.0,
            revisit_triggers=[],
        )
        s.commit()
    finally:
        s.close()

    d1 = (event_at + timedelta(days=1)).date().isoformat()
    d2 = (event_at + timedelta(days=15)).date().isoformat()
    # +5% end-of-window drift → neutral direction → expired_positive.
    bars = _bars(
        (d1, 100.0, 101.0, 99.0, 100.0),
        (d2, 104.0, 106.0, 103.0, 105.0),
    )

    s = session_factory()
    try:
        summary = run_evaluator_batch(
            s, now=now, price_fetcher=_price_fetcher(bars)
        )
        s.commit()
        assert summary.evaluated == 1
        outcome = s.query(PredictionOutcome).one()
        assert outcome.outcome_kind == "expired_positive"
    finally:
        s.close()
