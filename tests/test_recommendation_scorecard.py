from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from argosy.services.predictions.writers import DEEP_DECISION_VERDICT_SOURCE
from argosy.services.recommendation_scorecard import build_recommendation_scorecard
from argosy.state.models import (
    ActionProposal,
    Base,
    Prediction,
    PredictionOutcome,
    Proposal,
    ScanState,
    User,
    Verdict,
)


def test_unexecuted_recommendation_remains_in_both_horizon_scorecards() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    event_at = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as db:
        db.add(User(id="ariel"))
        verdict = Verdict(
            user_id="ariel", subject="GLUE", verdict="BUY", conviction="MED",
            source_decision_run_id=491, settled=True, created_at=event_at,
        )
        db.add(verdict)
        db.flush()
        proposal = Proposal(
            user_id="ariel", ticker="GLUE", action="buy",
            size_shares_or_currency=26_000, size_units="currency", tier="T2",
            account_class="main", status="cancelled", source="decision_funnel",
            shadow=0, decision_run_id=491, expected_impact_json="{}",
            created_at=event_at, updated_at=event_at,
        )
        db.add(proposal)
        for horizon in (30, 180):
            prediction = Prediction(
                user_id="ariel", source=DEEP_DECISION_VERDICT_SOURCE,
                source_ref=json.dumps({
                    "verdict_id": verdict.id, "subject": "GLUE",
                    "verdict": "BUY", "horizon_days": horizon,
                }),
                ticker="GLUE", direction="long", entry_price=10,
                timeframe_days=horizon, message_id=f"glue-{horizon}",
                event_at=event_at,
                evaluation_due_at=event_at + timedelta(days=horizon),
                evaluation_method=(
                    "fixed_lookahead_180d" if horizon == 180 else "fixed_lookahead_30d"
                ),
            )
            db.add(prediction)
            db.flush()
            if horizon == 30:
                db.add(PredictionOutcome(
                    prediction_id=prediction.id, outcome_kind="expired_positive",
                    pnl_pct=0.20, evaluated_at=event_at + timedelta(days=30),
                    evaluation_method="fixed_lookahead_30d",
                ))
        db.commit()

        payload = build_recommendation_scorecard(db, user_id="ariel")

    assert payload["horizons"]["30d"]["wins"] == 1
    assert payload["horizons"]["180d"]["scheduled"] == 1
    assert payload["horizons"]["180d"]["graded"] == 0
    assert {row["disposition"] for row in payload["recent"]} == {
        "reconciled_not_selected"
    }
    assert payload["coverage"]["with_180d_forecast"] == 1
    assert payload["coverage"]["universe_recall_status"] == "radar_only"
    engine.dispose()


def test_surfaced_order_is_scored_even_when_user_does_not_buy() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    event_at = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as db:
        db.add(User(id="ariel"))
        recommendation = ActionProposal(
            user_id="ariel",
            summary="Buy QURE",
            rationale_md="A displayed but declined trade plan.",
            suggested_payload="{}",
            severity="info",
            surfaced_at=event_at,
            expires_at=event_at + timedelta(days=7),
            status="rejected",
            kind="allocate",
            dedup_key="scorecard:qure",
            execution_state="proposed",
        )
        db.add(recommendation)
        db.flush()
        prediction = Prediction(
            user_id="ariel",
            source="signal_stream:order_sheet",
            source_ref=json.dumps({
                "proposal_id": recommendation.id,
                "action": "BUY",
                "horizon_days": 180,
            }),
            ticker="QURE",
            direction="long",
            entry_price=20,
            timeframe_days=180,
            message_id="sheet-qure-180",
            event_at=event_at,
            evaluation_due_at=event_at + timedelta(days=180),
            evaluation_method="fixed_lookahead_180d",
        )
        db.add(prediction)
        db.flush()
        db.add(PredictionOutcome(
            prediction_id=prediction.id,
            outcome_kind="expired_positive",
            pnl_pct=0.40,
            evaluated_at=event_at + timedelta(days=180),
            evaluation_method="fixed_lookahead_180d",
        ))
        db.commit()

        payload = build_recommendation_scorecard(db, user_id="ariel")

    surfaced = payload["surfaced_order_sheets"]
    assert surfaced["horizons"]["180d"]["wins"] == 1
    assert surfaced["recent"][0]["disposition"] == "declined"
    assert surfaced["recent"][0]["ticker"] == "QURE"
    engine.dispose()


def test_radar_winner_not_recommended_is_visible_as_missed_opportunity() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    event_at = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as db:
        db.add(User(id="ariel"))
        db.add(ScanState(user_id="ariel", ticker="MISS", status="active"))
        prediction = Prediction(
            user_id="ariel",
            source="signal_stream:radar_observation",
            source_ref=json.dumps({"stream": "radar_observation"}),
            ticker="MISS",
            direction="long",
            entry_price=10,
            timeframe_days=180,
            message_id="radar-miss-180",
            event_at=event_at,
            evaluation_due_at=event_at + timedelta(days=180),
            evaluation_method="fixed_lookahead_180d",
        )
        db.add(prediction)
        db.flush()
        db.add(PredictionOutcome(
            prediction_id=prediction.id,
            outcome_kind="expired_positive",
            pnl_pct=0.80,
            evaluated_at=event_at + timedelta(days=180),
            evaluation_method="fixed_lookahead_180d",
        ))
        db.commit()

        payload = build_recommendation_scorecard(db, user_id="ariel")

    radar = payload["radar_opportunities"]
    assert radar["winners_180d"] == 1
    assert radar["missed_winners_180d"] == 1
    assert radar["recent_missed_winners"][0]["ticker"] == "MISS"
    assert payload["coverage"]["radar_clock_status"] == "complete"
    assert payload["coverage"]["radar_clock_coverage_pct"] == 100.0
    engine.dispose()


def test_radar_clock_coverage_reports_missing_active_names_honestly() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    event_at = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as db:
        db.add(User(id="ariel"))
        db.add_all([
            ScanState(user_id="ariel", ticker="CLOCKED", status="active"),
            ScanState(user_id="ariel", ticker="MISSING", status="active"),
        ])
        db.add(Prediction(
            user_id="ariel",
            source="signal_stream:radar_observation",
            source_ref=json.dumps({
                "stream": "radar_observation",
                "horizon_days": 180,
            }),
            ticker="CLOCKED",
            direction="long",
            entry_price=10,
            timeframe_days=180,
            message_id="radar-clocked-180",
            event_at=event_at,
            evaluation_due_at=event_at + timedelta(days=180),
            evaluation_method="fixed_lookahead_180d",
        ))
        db.commit()

        payload = build_recommendation_scorecard(db, user_id="ariel")

    coverage = payload["coverage"]
    assert coverage["eligible_radar_names"] == 2
    assert coverage["radar_names_with_180d_clock"] == 1
    assert coverage["radar_clock_coverage_pct"] == 50.0
    assert coverage["radar_clock_status"] == "partial"
    assert coverage["radar_names_missing_180d_clock"] == ["MISSING"]
    assert "Only 1 of 2" in coverage["universe_recall_reason"]
    engine.dispose()
