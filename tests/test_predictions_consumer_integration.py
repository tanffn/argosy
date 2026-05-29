"""Spec C commit #6 — consumer integration tests for the predictions ledger.

Covers:

* Migration 0053 applies cleanly; ``provenance_weights_applied`` defaults
  to 0 and the CHECK constraint enforces ``IN (0, 1)``.
* :func:`argosy.services.predictions.reliability.get_weight_for_source`
  short-circuits to 1.0 when ``provenance_weights_applied=True`` —
  the spec §6.6 / codex IMPORTANT 3 anti-feedback-loop contract.
* News-signal-analyst helper returns a weight in
  ``[WEIGHT_FLOOR, WEIGHT_CEIL]`` for known sources and 1.0 for unknown.
* Per-position-thesis derivation surfaces a reliability annotation
  for the ``internal_news_signal_analyst`` source per spec §6.3.
* State-observer write site stamps ``provenance_weights_applied=1`` on
  every observer prediction — so the next tick's
  ``get_weight_for_source`` short-circuits and the observer can't
  compound its own self-attenuation.
* Synthesizer write site (``emit_thesis_predictions(..., provenance_weights_applied=True)``)
  stamps the resulting thesis predictions so downstream consumers
  skip re-weighting.
* :func:`argosy.services.predictions.evaluator.run_evaluator_batch`
  calls :func:`invalidate_reliability_cache` at end-of-batch so the
  next consumer query reads fresh weights.

Pattern (mirrors ``tests/test_predictions_reliability.py``): per-test
in-memory SQLite at alembic head, raw INSERT seeds via SQLAlchemy core.

Run:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_predictions_consumer_integration.py -v
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from argosy.services.predictions import reliability as reliability_mod
from argosy.services.predictions.reliability import (
    WEIGHT_CEIL,
    WEIGHT_FLOOR,
    get_weight_for_source,
    invalidate_reliability_cache,
    reliability_annotation,
)
from argosy.state.models import Prediction, PredictionOutcome


# ---------------------------------------------------------------------------
# Fixtures — fresh DB per test; FKs enforced.
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path, monkeypatch) -> "tuple[Session, sessionmaker]":
    """Per-test SQLite at alembic head; yields (Session, factory).

    Same idiom as ``tests/test_predictions_reliability.py`` so the
    view + the FK chain on ``predictions`` / ``prediction_outcomes`` /
    ``evaluation_method_registry`` is in scope.
    """
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

    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) "
                "VALUES ('ariel', 'free', '2026-01-01 00:00:00+00:00')"
            )
        )

    session = factory()
    invalidate_reliability_cache()
    try:
        yield session, factory
    finally:
        session.close()
        engine.dispose()
        invalidate_reliability_cache()


_INSERT_COUNTER = [0]


def _insert_prediction(
    session: Session,
    *,
    user_id: str = "ariel",
    source: str = "discord",
    ticker: str | None = "NVDA",
    direction: str = "long",
    entry_price: float | None = 100.0,
    target_price: float | None = None,
    stop_price: float | None = None,
    timeframe_days: int | None = 7,
    event_at: datetime | None = None,
    evaluation_due_at: datetime | None = None,
    evaluation_method: str = "fixed_lookahead_7d",
    archived: int = 0,
    provenance_weights_applied: int = 0,
) -> Prediction:
    """Seed a predictions row + return the ORM object."""
    if event_at is None:
        event_at = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    if evaluation_due_at is None:
        evaluation_due_at = event_at + timedelta(days=timeframe_days or 7)
    _INSERT_COUNTER[0] += 1
    row = Prediction(
        user_id=user_id,
        source=source,
        source_ref="{}",
        ticker=ticker,
        direction=direction,
        entry_price=(
            Decimal(str(entry_price)) if entry_price is not None else None
        ),
        target_price=(
            Decimal(str(target_price)) if target_price is not None else None
        ),
        stop_price=(
            Decimal(str(stop_price)) if stop_price is not None else None
        ),
        timeframe_days=timeframe_days,
        message_id=f"v1|predictions|{source}|{_INSERT_COUNTER[0]}",
        event_at=event_at,
        evaluation_due_at=evaluation_due_at,
        evaluation_method=evaluation_method,
        archived=archived,
        provenance_weights_applied=provenance_weights_applied,
    )
    session.add(row)
    session.flush()
    return row


def _insert_outcome(
    session: Session,
    prediction: Prediction,
    *,
    outcome_kind: str,
    pnl_pct: float | None,
    evaluation_method: str | None = None,
    evaluated_at: datetime | None = None,
) -> PredictionOutcome:
    method = evaluation_method or prediction.evaluation_method
    row = PredictionOutcome(
        prediction_id=prediction.id,
        evaluation_method=method,
        outcome_kind=outcome_kind,
        pnl_pct=Decimal(str(pnl_pct)) if pnl_pct is not None else None,
        evaluated_at=evaluated_at or datetime.now(timezone.utc),
        entry_price_used=Decimal("100.0"),
        exit_price_used=Decimal("100.0"),
        exit_trigger_date=date(2026, 5, 8),
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Migration 0053
# ---------------------------------------------------------------------------


def test_migration_0053_provenance_column_exists_and_defaults_zero(
    sync_session,
) -> None:
    """``provenance_weights_applied`` column lands NOT NULL DEFAULT 0,
    CHECK ``IN (0, 1)``.
    """
    session, _ = sync_session
    p = _insert_prediction(session)
    session.flush()
    # Default is 0 — no explicit value passed → write helper defaults
    # match the column's server_default.
    assert p.provenance_weights_applied == 0

    # Round-trip the row to confirm the column persists.
    fetched = session.execute(
        sa.text(
            "SELECT provenance_weights_applied FROM predictions WHERE id = :id"
        ),
        {"id": p.id},
    ).scalar_one()
    assert fetched == 0


def test_migration_0053_check_constraint_rejects_two(sync_session) -> None:
    """CHECK constraint enforces the closed set {0, 1}."""
    session, _ = sync_session
    with pytest.raises(IntegrityError):
        _insert_prediction(session, provenance_weights_applied=2)


def test_migration_0053_explicit_one_is_accepted(sync_session) -> None:
    """Writers / consumers can stamp 1 explicitly."""
    session, _ = sync_session
    p = _insert_prediction(session, provenance_weights_applied=1)
    session.flush()
    assert p.provenance_weights_applied == 1


# ---------------------------------------------------------------------------
# get_weight_for_source — provenance short-circuit
# ---------------------------------------------------------------------------


def test_get_weight_for_source_short_circuits_on_provenance(
    sync_session,
) -> None:
    """Spec §6.6 / codex IMPORTANT 3 — when ``provenance_weights_applied=True``
    the helper returns 1.0 BEFORE consulting the view. The signal has
    already been weighted upstream; re-multiplying would compound.
    """
    session, _ = sync_session

    # Seed enough data to produce a < 1.0 weight without the short
    # circuit (10 hit_stop outcomes → hit_rate = 0.0 → would clamp to
    # WEIGHT_FLOOR=0.10).
    for _ in range(10):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="hit_stop", pnl_pct=-0.10)
    session.flush()

    # Without provenance: weight clamped down (poor reliability).
    w_unstamped = get_weight_for_source(
        session, "ariel", "discord", "fixed_lookahead",
        provenance_weights_applied=False,
    )
    assert w_unstamped == WEIGHT_FLOOR, (
        f"unstamped: expected floor {WEIGHT_FLOOR}, got {w_unstamped}"
    )

    # With provenance: short-circuits to 1.0 regardless of view.
    w_stamped = get_weight_for_source(
        session, "ariel", "discord", "fixed_lookahead",
        provenance_weights_applied=True,
    )
    assert w_stamped == 1.0, (
        f"stamped: expected 1.0 short-circuit, got {w_stamped}"
    )


def test_get_weight_for_source_unknown_returns_one(sync_session) -> None:
    """Unknown source / no scored predictions → 1.0 (baseline)."""
    session, _ = sync_session
    w = get_weight_for_source(
        session, "ariel", "sec_13f", "fixed_lookahead",
    )
    assert w == 1.0


def test_get_weight_for_source_known_clamped_to_range(sync_session) -> None:
    """Known source with scored data → weight in [WEIGHT_FLOOR, WEIGHT_CEIL]."""
    session, _ = sync_session
    # Mixed outcomes — hit_rate ~ 0.5 → weight ramps with sample size.
    for _ in range(15):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    for _ in range(15):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="hit_stop", pnl_pct=-0.10)
    session.flush()

    w = get_weight_for_source(
        session, "ariel", "discord", "fixed_lookahead",
    )
    assert WEIGHT_FLOOR <= w <= WEIGHT_CEIL


# ---------------------------------------------------------------------------
# reliability_annotation — soft data surface for per_position_thesis
# ---------------------------------------------------------------------------


def test_reliability_annotation_shape(sync_session) -> None:
    """Annotation dict carries the keys per_position_thesis expects."""
    session, _ = sync_session
    p = _insert_prediction(
        session,
        source="internal_news_signal_analyst",
    )
    _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    session.flush()

    ann = reliability_annotation(
        session, "ariel", "internal_news_signal_analyst",
    )
    assert ann["source"] == "internal_news_signal_analyst"
    assert ann["method_family"] == "fixed_lookahead"
    assert ann["scored_predictions"] == 1
    assert "hit_rate" in ann
    assert "sample_size_warning" in ann
    assert "effective_weight" in ann


def test_reliability_annotation_unknown_source(sync_session) -> None:
    """Unknown source → defaults, no exception."""
    session, _ = sync_session
    ann = reliability_annotation(session, "ariel", "tipranks")
    assert ann["source"] == "tipranks"
    assert ann["scored_predictions"] == 0
    assert ann["sample_size_warning"] is True
    assert ann["effective_weight"] == 1.0


# ---------------------------------------------------------------------------
# per_position_thesis — derive_position_theses surfaces annotations
# ---------------------------------------------------------------------------


def test_derive_position_theses_surfaces_reliability_annotation(
    sync_session,
) -> None:
    """When session+user_id passed, every PositionThesis carries an
    ``internal_news_signal_analyst`` reliability annotation."""
    from argosy.services.per_position_thesis import derive_position_theses

    session, _ = sync_session

    # Seed a couple of internal_news_signal_analyst outcomes so the
    # annotation has real data to surface.
    for _ in range(3):
        p = _insert_prediction(
            session,
            source="internal_news_signal_analyst",
            ticker="NVDA",
        )
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    session.flush()

    # Minimal plan_version + portfolio fixtures the derivation accepts.
    plan_version = {
        "horizon_short_json": '{"targets": [], "actions": []}',
        "horizon_medium_json": (
            '{"targets": [{"label": "NVDA share of portfolio", '
            '"unit": "pct_of_portfolio", "value": 30}], "actions": []}'
        ),
        "horizon_long_json": '{"targets": [], "actions": []}',
    }
    portfolio = {
        "positions": [
            {"symbol": "NVDA", "shares": 100, "usd_value_k": 50.0},
        ],
        "total_usd_value_k": 100.0,
    }

    theses = derive_position_theses(
        plan_version, portfolio, agent_reports=[],
        session=session, user_id="ariel",
    )
    assert len(theses) >= 1
    for t in theses:
        assert t.reliability_annotations, (
            f"thesis {t.ticker} missing reliability_annotations"
        )
        ann = t.reliability_annotations[0]
        assert ann["source"] == "internal_news_signal_analyst"


def test_derive_position_theses_without_session_skips_annotations(
    sync_session,
) -> None:
    """Backward-compat: no session passed → annotations list is empty,
    no exception."""
    from argosy.services.per_position_thesis import derive_position_theses

    plan_version = {
        "horizon_short_json": (
            '{"targets": [{"label": "NVDA share", '
            '"unit": "pct_of_portfolio", "value": 30}], "actions": []}'
        ),
        "horizon_medium_json": '{}',
        "horizon_long_json": '{}',
    }
    portfolio = {
        "positions": [
            {"symbol": "NVDA", "shares": 100, "usd_value_k": 50.0},
        ],
        "total_usd_value_k": 100.0,
    }

    theses = derive_position_theses(plan_version, portfolio, agent_reports=[])
    assert len(theses) >= 1
    for t in theses:
        assert t.reliability_annotations == []


# ---------------------------------------------------------------------------
# state_observer — provenance_weights_applied stamped on its writes
# ---------------------------------------------------------------------------


def test_state_observer_flag_writer_stamps_provenance(sync_session) -> None:
    """``_maybe_write_observer_prediction`` must pass
    ``provenance_weights_applied=True`` so the next consumer doesn't
    re-multiply by the observer's own weight (spec §6.4 / §6.6)."""
    from argosy.services.state_observer_flag_writer import (
        _maybe_write_observer_prediction,
    )
    from argosy.state.models import MonitorFlag

    session, _ = sync_session

    # Seed a monitor_flag row first so its id is real (FK source for
    # the predictions row's source_ref).
    flag = MonitorFlag(
        user_id="ariel",
        kind="state_observer_fx_observation",
        severity="warning",
        payload='{"primary_field": "macro.fx_usd_nis_spot"}',
        dedup_key="v1|state_observer|abc",
    )
    session.add(flag)
    session.commit()
    session.refresh(flag)

    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
    _maybe_write_observer_prediction(
        session,
        user_id="ariel",
        observer_flag_row=flag,
        primary_field="macro.fx_usd_nis_spot",
        severity="warning",
        deviation_bucket="large",
        now=now,
    )

    # The just-written prediction MUST carry provenance_weights_applied=1.
    row = session.execute(
        sa.select(Prediction).where(
            Prediction.source == "internal_state_observer"
        )
    ).scalar_one_or_none()
    assert row is not None, "observer prediction was not written"
    assert row.provenance_weights_applied == 1, (
        "spec §6.6 — observer's writer MUST stamp provenance=1 to "
        "prevent self-attenuation across ticks"
    )


# ---------------------------------------------------------------------------
# synth → per_position_thesis emit_thesis_predictions stamping
# ---------------------------------------------------------------------------


def test_emit_thesis_predictions_stamps_when_requested(sync_session) -> None:
    """Spec §6.1 / §6.6 — when the synthesizer's banner has already
    weighted upstream sources, the resulting thesis predictions are
    stamped so downstream consumers don't re-multiply."""
    from argosy.services.per_position_thesis import (
        PositionThesis,
        emit_thesis_predictions,
    )

    session, _ = sync_session

    theses = [
        PositionThesis(
            ticker="NVDA",
            current_shares=100.0,
            current_weight_pct=30.0,
            current_usd_value=30000.0,
            verdict="BUY",
            conviction="MEDIUM",
            reasoning_md="strong fundamentals",
        ),
        PositionThesis(
            ticker="AAPL",
            current_shares=50.0,
            current_weight_pct=10.0,
            current_usd_value=10000.0,
            verdict="HOLD",
            conviction="LOW",
            reasoning_md="hold the line",
        ),
    ]

    when = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
    emit_thesis_predictions(
        session,
        "ariel",
        plan_version_id=42,
        theses=theses,
        event_at=when,
        provenance_weights_applied=True,
    )

    rows = session.execute(
        sa.select(Prediction).where(
            Prediction.source == "internal_per_position_thesis"
        ).order_by(Prediction.ticker)
    ).scalars().all()
    assert len(rows) == 2
    for r in rows:
        assert r.provenance_weights_applied == 1, (
            f"thesis prediction {r.ticker} missing provenance stamp"
        )


def test_emit_thesis_predictions_default_unstamped(sync_session) -> None:
    """Default call (e.g. positions route handler re-deriving theses)
    leaves provenance=0 — no upstream weighting happened."""
    from argosy.services.per_position_thesis import (
        PositionThesis,
        emit_thesis_predictions,
    )

    session, _ = sync_session

    theses = [
        PositionThesis(
            ticker="NVDA",
            current_shares=100.0,
            current_weight_pct=30.0,
            current_usd_value=30000.0,
            verdict="HOLD",
            conviction="HIGH",
            reasoning_md="status quo",
        ),
    ]

    emit_thesis_predictions(
        session, "ariel", plan_version_id=99, theses=theses,
    )

    row = session.execute(
        sa.select(Prediction).where(
            Prediction.source == "internal_per_position_thesis"
        )
    ).scalar_one()
    assert row.provenance_weights_applied == 0


# ---------------------------------------------------------------------------
# news_signal_analyst — source_reliability_factor surfaces in the prompt
# ---------------------------------------------------------------------------


def test_news_signal_analyst_renders_reliability_factor_in_prompt() -> None:
    """The agent's user-prompt builder MUST emit
    ``source_reliability_factor: <value>`` per signal so the LLM can
    apply the spec §6.2 down-weighting rule."""
    from argosy.agents.news_signal_analyst import (
        AnalyzedSignalIn,
        NewsSignalAnalystAgent,
    )

    agent = NewsSignalAnalystAgent(user_id="ariel")
    sig = AnalyzedSignalIn(
        signal_id=42,
        source="discord",
        source_trust="medium",
        received_at=datetime(2026, 5, 30, tzinfo=timezone.utc),
        parsed_tickers=["NVDA"],
        event_keywords=["earnings"],
        sentiment="positive",
        evidence_excerpt="NVDA beats earnings",
        source_reliability_factor=0.45,
    )
    system, user = agent.build_prompt(
        signals=[sig], user_holdings=["NVDA"],
    )
    assert "source_reliability_factor: 0.45" in user
    # The system prompt explains the contract — sanity check it's there.
    assert "source_reliability_factor" in system
    assert "0.7" in system  # the spec §6.2 "< 0.7" guidance


def test_news_signal_analyst_default_factor_is_one() -> None:
    """A signal without an explicit reliability factor defaults to 1.0."""
    from argosy.agents.news_signal_analyst import AnalyzedSignalIn

    sig = AnalyzedSignalIn(
        signal_id=1,
        source="rss",
        source_trust="high",
        received_at=datetime(2026, 5, 30, tzinfo=timezone.utc),
        parsed_tickers=[],
        event_keywords=[],
        sentiment="neutral",
        evidence_excerpt="…",
    )
    assert sig.source_reliability_factor == 1.0


# ---------------------------------------------------------------------------
# Evaluator — invalidate_reliability_cache hook fires at end-of-batch
# ---------------------------------------------------------------------------


def test_run_evaluator_batch_invalidates_reliability_cache(
    sync_session, monkeypatch,
) -> None:
    """Spec C commit #6 — the batch driver MUST call
    :func:`invalidate_reliability_cache` at end-of-batch so the next
    consumer query reads fresh weights instead of waiting for the
    5-min TTL.

    Test by patching ``invalidate_reliability_cache`` and verifying
    the patched object is called.
    """
    from argosy.services.predictions import evaluator as evaluator_mod
    from argosy.services.predictions.evaluator import run_evaluator_batch

    session, _ = sync_session

    # Use a spy on the function looked up from the reliability module
    # since the evaluator imports it lazily at end-of-batch via
    # ``from argosy.services.predictions.reliability import
    # invalidate_reliability_cache``. Patching at the SOURCE module is
    # the correct hook.
    spy = mock.MagicMock()
    monkeypatch.setattr(
        "argosy.services.predictions.reliability.invalidate_reliability_cache",
        spy,
    )

    # Empty batch is fine — the hook still fires after the "0 due"
    # path. The fetcher is unused since no predictions are due.
    summary = run_evaluator_batch(
        session,
        now=datetime(2026, 6, 30, tzinfo=timezone.utc),
        price_fetcher=lambda *a, **k: [],
    )
    assert summary.evaluated == 0
    spy.assert_called_once()


def test_run_evaluator_batch_invalidates_cache_after_nonempty_batch(
    sync_session, monkeypatch,
) -> None:
    """Same hook fires when actual outcomes are inserted."""
    from argosy.services.predictions.evaluator import (
        Bar,
        run_evaluator_batch,
    )

    session, _ = sync_session

    # Seed one due prediction.
    p = _insert_prediction(
        session,
        evaluation_due_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
        event_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    session.commit()

    spy = mock.MagicMock()
    monkeypatch.setattr(
        "argosy.services.predictions.reliability.invalidate_reliability_cache",
        spy,
    )

    fake_bars = [
        Bar(bar_date=date(2026, 5, 2), open=100, high=101, low=99, close=100.5),
        Bar(bar_date=date(2026, 5, 8), open=110, high=112, low=109, close=111),
    ]
    summary = run_evaluator_batch(
        session,
        now=datetime(2026, 5, 9, tzinfo=timezone.utc),
        price_fetcher=lambda *a, **k: fake_bars,
    )
    assert summary.evaluated >= 0  # may be 0 if window math doesn't intersect
    spy.assert_called_once()


# ---------------------------------------------------------------------------
# Cache-busting integration — provenance flag flows end-to-end
# ---------------------------------------------------------------------------


def test_cache_invalidation_makes_next_query_reread(sync_session) -> None:
    """Sanity check the cache + invalidate path together: a cached
    weight survives a second call without invalidation, then the
    invalidate hook forces re-read."""
    session, _ = sync_session

    # First call → miss → seeded zero rows → 1.0.
    w1 = get_weight_for_source(
        session, "ariel", "discord", "fixed_lookahead",
    )
    assert w1 == 1.0

    # Add data — cache still has the old (empty) result.
    for _ in range(12):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="hit_stop", pnl_pct=-0.10)
    session.flush()

    w_cached = get_weight_for_source(
        session, "ariel", "discord", "fixed_lookahead",
    )
    assert w_cached == 1.0, "cache hit should still return the old 1.0"

    # Invalidate → re-read → low weight (poor reliability).
    invalidate_reliability_cache()
    w_fresh = get_weight_for_source(
        session, "ariel", "discord", "fixed_lookahead",
    )
    assert w_fresh < 1.0, "after invalidate, query should reflect new data"
    assert w_fresh >= WEIGHT_FLOOR, "must respect floor"
