from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.predictions import reliability
from argosy.state.models import (
    Base,
    EvaluationMethod,
    Prediction,
    PredictionOutcome,
    User,
)


@pytest.fixture
def db():
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    session.add(User(id="ariel", plan="free"))
    session.add_all(
        [
            EvaluationMethod(
                method_name="fixed_lookahead_30d",
                family="fixed_lookahead",
                method_version=1,
                is_active=1,
            ),
            EvaluationMethod(
                method_name="fixed_lookahead_30d_entry_backfilled",
                family="fixed_lookahead",
                method_version=2,
                is_active=1,
            ),
            EvaluationMethod(
                method_name="fixed_lookahead_180d",
                family="fixed_lookahead",
                method_version=1,
                is_active=1,
            ),
            EvaluationMethod(
                method_name="fixed_lookahead_180d_entry_backfilled",
                family="fixed_lookahead",
                method_version=2,
                is_active=1,
            ),
        ]
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _prediction(
    db,
    *,
    timeframe_days: int,
    direction: str = "long",
    event_at: datetime | None = None,
    source: str = "signal_stream:gov_contracts",
) -> Prediction:
    event_at = event_at or datetime(2026, 1, 1, tzinfo=UTC)
    method = f"fixed_lookahead_{timeframe_days}d"
    prediction = Prediction(
        user_id="ariel",
        source=source,
        source_ref="{}",
        ticker="TEST",
        direction=direction,
        entry_price=Decimal("100"),
        timeframe_days=timeframe_days,
        message_id=f"{timeframe_days}:{direction}:{len(db.new)}:{event_at.isoformat()}",
        event_at=event_at,
        evaluation_due_at=event_at + timedelta(days=timeframe_days),
        evaluation_method=method,
    )
    db.add(prediction)
    db.flush()
    return prediction


def test_warning_only_source_predictions_contribute_to_scorecard(db) -> None:
    prediction = _prediction(
        db,
        timeframe_days=30,
        direction="short",
        source="signal_stream:insider_cluster",
    )
    _outcome(
        db,
        prediction,
        outcome_kind="hit_target",
        pnl_pct=0.12,
        entry=100,
        exit=88,
    )
    db.commit()

    scorecard = reliability.signal_source_scorecard(
        db,
        "ariel",
        "insider_cluster",
        now=datetime(2026, 7, 10, tzinfo=UTC),
    )

    assert scorecard["source"] == "signal_stream:insider_cluster"
    assert scorecard["scored_outcomes"] == 1
    assert scorecard["win_rate"] == pytest.approx(1.0)
    assert scorecard["horizons"]["30d"]["scored_outcomes"] == 1


def _outcome(
    db,
    prediction: Prediction,
    *,
    outcome_kind: str,
    pnl_pct: float | None,
    entry: float | None,
    exit: float | None,
    method: str | None = None,
    evaluated_at: datetime | None = None,
) -> PredictionOutcome:
    outcome = PredictionOutcome(
        prediction_id=prediction.id,
        evaluation_method=method or prediction.evaluation_method,
        outcome_kind=outcome_kind,
        pnl_pct=Decimal(str(pnl_pct)) if pnl_pct is not None else None,
        evaluated_at=evaluated_at or datetime(2026, 7, 1, tzinfo=UTC),
        entry_price_used=Decimal(str(entry)) if entry is not None else None,
        exit_price_used=Decimal(str(exit)) if exit is not None else None,
    )
    db.add(outcome)
    db.flush()
    return outcome


def test_kill_policy_uses_only_complete_180d_boundary_and_equality() -> None:
    policy_fn = getattr(reliability, "signal_funnel_context_policy", None)
    assert policy_fn is not None, "pure signal kill policy is not implemented"

    below = policy_fn(
        scored_180d=49,
        win_rate_180d=0.40,
        always_long_same_tickers_win_rate=0.40,
    )
    assert below.funnel_context_enabled is True
    assert below.calibrated is False
    assert below.kill_reason is None

    above_benchmark = policy_fn(
        scored_180d=50,
        win_rate_180d=0.51,
        always_long_same_tickers_win_rate=0.50,
    )
    assert above_benchmark.funnel_context_enabled is True
    assert above_benchmark.calibrated is True

    equality = policy_fn(
        scored_180d=50,
        win_rate_180d=0.50,
        always_long_same_tickers_win_rate=0.50,
    )
    assert equality.funnel_context_enabled is False
    assert equality.calibrated is True
    assert "does not beat always-long" in equality.kill_reason

    unverifiable = policy_fn(
        scored_180d=50,
        win_rate_180d=0.80,
        always_long_same_tickers_win_rate=None,
    )
    assert unverifiable.funnel_context_enabled is True
    assert unverifiable.calibrated is False
    assert unverifiable.kill_reason is None


def test_scorecard_separates_horizons_supersedes_and_benchmarks_raw_long(db) -> None:
    p30 = _prediction(db, timeframe_days=30)
    _outcome(
        db,
        p30,
        outcome_kind="hit_target",
        pnl_pct=0.10,
        entry=100,
        exit=110,
    )
    unparseable = _prediction(db, timeframe_days=30)
    _outcome(
        db,
        unparseable,
        outcome_kind="unparseable",
        pnl_pct=None,
        entry=None,
        exit=None,
    )

    short = _prediction(db, timeframe_days=180, direction="short")
    _outcome(
        db,
        short,
        outcome_kind="hit_target",
        pnl_pct=0.20,
        entry=100,
        exit=80,
    )
    superseded = _prediction(db, timeframe_days=180)
    _outcome(
        db,
        superseded,
        outcome_kind="hit_stop",
        pnl_pct=-0.10,
        entry=100,
        exit=90,
        method="fixed_lookahead_180d",
        evaluated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    _outcome(
        db,
        superseded,
        outcome_kind="expired_positive",
        pnl_pct=0.05,
        entry=100,
        exit=105,
        method="fixed_lookahead_180d_entry_backfilled",
        evaluated_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    db.commit()

    scorecard = reliability.signal_source_scorecard(
        db,
        "ariel",
        "gov_contracts",
        now=datetime(2026, 7, 10, tzinfo=UTC),
    )

    assert scorecard["scored_outcomes"] == 3
    assert scorecard["win_rate"] == pytest.approx(1.0)
    assert scorecard["avg_pnl_pct"] == pytest.approx((0.10 + 0.20 + 0.05) / 3)
    assert scorecard["horizons"]["30d"] == {
        "scored_outcomes": 1,
        "win_rate": pytest.approx(1.0),
        "avg_pnl_pct": pytest.approx(0.10),
    }
    assert scorecard["horizons"]["180d"]["scored_outcomes"] == 2
    assert scorecard["horizons"]["180d"]["win_rate"] == pytest.approx(1.0)
    assert scorecard["horizons"]["180d"]["avg_pnl_pct"] == pytest.approx(0.125)
    assert scorecard["horizons"]["180d"][
        "always_long_same_tickers_win_rate"
    ] == pytest.approx(0.5)
    assert scorecard["funnel_context_enabled"] is True
    assert scorecard["kill_reason"] is None


def test_scorecard_ignores_higher_version_inactive_outcome(db) -> None:
    db.add(
        EvaluationMethod(
            method_name="fixed_lookahead_180d_inactive_v3",
            family="fixed_lookahead",
            method_version=3,
            is_active=0,
        )
    )
    prediction = _prediction(db, timeframe_days=180)
    _outcome(
        db,
        prediction,
        outcome_kind="hit_target",
        pnl_pct=0.10,
        entry=100,
        exit=110,
        method="fixed_lookahead_180d",
    )
    _outcome(
        db,
        prediction,
        outcome_kind="hit_stop",
        pnl_pct=-0.20,
        entry=100,
        exit=80,
        method="fixed_lookahead_180d_inactive_v3",
    )
    db.commit()

    scorecard = reliability.signal_source_scorecard(
        db, "ariel", "gov_contracts"
    )

    assert scorecard["horizons"]["180d"]["scored_outcomes"] == 1
    assert scorecard["horizons"]["180d"]["win_rate"] == pytest.approx(1.0)
    assert scorecard["horizons"]["180d"]["avg_pnl_pct"] == pytest.approx(0.10)


def test_always_long_exactly_positive_one_percent_is_a_win(db) -> None:
    prediction = _prediction(db, timeframe_days=180)
    _outcome(
        db,
        prediction,
        outcome_kind="expired_positive",
        pnl_pct=0.01,
        entry=100,
        exit=101,
    )
    db.commit()

    scorecard = reliability.signal_source_scorecard(
        db, "ariel", "gov_contracts"
    )

    assert scorecard["horizons"]["180d"][
        "always_long_same_tickers_win_rate"
    ] == pytest.approx(1.0)


def test_scorecard_zero_entry_never_crashes_and_benchmark_is_unverifiable(db) -> None:
    prediction = _prediction(db, timeframe_days=180)
    _outcome(
        db,
        prediction,
        outcome_kind="expired_positive",
        pnl_pct=0.03,
        entry=0,
        exit=103,
    )
    db.commit()

    scorecard = reliability.signal_source_scorecard(
        db, "ariel", "gov_contracts"
    )

    assert scorecard["horizons"]["180d"]["scored_outcomes"] == 1
    assert scorecard["horizons"]["180d"][
        "always_long_same_tickers_win_rate"
    ] is None
    assert scorecard["funnel_context_enabled"] is True
    assert scorecard["calibration"].startswith("uncalibrated (beta")
