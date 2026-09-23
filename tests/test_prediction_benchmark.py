from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from argosy.services.predictions.benchmark import (
    BENCHMARK_VERSION,
    ensure_prediction_benchmark_outcomes,
)
from argosy.services.predictions.evaluator import Bar
from argosy.state.models import (
    Base,
    EvaluationMethod,
    Prediction,
    PredictionBenchmarkOutcome,
    PredictionOutcome,
    User,
)


def _seed(
    db: Session,
    *,
    verdict: str,
    direction: str,
    signed_pnl: float,
) -> PredictionOutcome:
    if db.get(User, "ariel") is None:
        db.add(User(id="ariel"))
        db.add(
            EvaluationMethod(
                method_name="fixed_lookahead_30d",
                family="fixed_lookahead",
                method_version=1,
                is_active=1,
            )
        )
        db.flush()
    prediction = Prediction(
        user_id="ariel",
        source="signal_stream:discovery_evaluation",
        source_ref=json.dumps({"verdict": verdict}),
        ticker="TEST",
        direction=direction,
        entry_price=Decimal("100"),
        timeframe_days=30,
        message_id=f"benchmark-{verdict}",
        event_at=datetime(2026, 1, 5, 14, tzinfo=UTC),
        evaluation_due_at=datetime(2026, 2, 4, 14, tzinfo=UTC),
        evaluation_method="fixed_lookahead_30d",
    )
    db.add(prediction)
    db.flush()
    outcome = PredictionOutcome(
        prediction_id=prediction.id,
        outcome_kind="expired_positive",
        pnl_pct=Decimal(str(signed_pnl)),
        evaluation_method="fixed_lookahead_30d",
        entry_price_used=Decimal("100"),
        exit_price_used=Decimal("112"),
        exit_trigger_date=date(2026, 2, 4),
    )
    db.add(outcome)
    db.flush()
    return outcome


def _bars(_ticker: str, _start: date, _end: date) -> list[Bar]:
    return [
        Bar(date(2026, 1, 2), 100, 100, 100, 100),
        Bar(date(2026, 2, 4), 105, 105, 105, 105),
    ]


def test_buy_records_subject_return_less_spy_return() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        outcome = _seed(db, verdict="BUY", direction="long", signed_pnl=0.12)
        summary = ensure_prediction_benchmark_outcomes(db, price_fetcher=_bars)
        row = db.scalar(select(PredictionBenchmarkOutcome))

        assert summary.compared == 1
        assert row is not None
        assert row.prediction_outcome_id == outcome.id
        assert row.benchmark_version == BENCHMARK_VERSION
        assert row.comparison_mode == "own_vs_benchmark"
        assert float(row.benchmark_return_pct) == 0.05
        assert float(row.subject_return_pct) == 0.12
        assert float(row.decision_excess_return_pct) == 0.07


def test_watch_scores_avoided_ticker_against_spy() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db, verdict="WATCH", direction="long", signed_pnl=0.12)
        ensure_prediction_benchmark_outcomes(db, price_fetcher=_bars)
        row = db.scalar(select(PredictionBenchmarkOutcome))

        assert row is not None
        assert row.comparison_mode == "avoid_vs_benchmark"
        assert float(row.decision_excess_return_pct) == -0.07


def test_benchmark_backfill_is_idempotent() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db, verdict="SELL", direction="short", signed_pnl=-0.12)
        first = ensure_prediction_benchmark_outcomes(db, price_fetcher=_bars)
        second = ensure_prediction_benchmark_outcomes(db, price_fetcher=_bars)

        assert first.compared == 1
        assert second.compared == 0
        assert db.scalar(select(PredictionBenchmarkOutcome.id)) is not None
