from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from argosy.services.predictions.writers import (
    DEEP_DECISION_VERDICT_SOURCE,
    DISCOVERY_EVALUATION_SOURCE,
    ensure_discovery_evaluation_predictions,
)
from argosy.services.recommendation_scorecard import build_recommendation_scorecard
from argosy.state.models import (
    ActionProposal,
    Base,
    EvaluationMethod,
    Prediction,
    PredictionBenchmarkOutcome,
    PredictionOutcome,
    Proposal,
    ScanState,
    User,
    Verdict,
)


# Production migrations seed the registry; metadata-only test databases must
# include it too, rather than relying on disabled SQLite foreign keys.
def _seed_scorecard_methods(metadata, connection, **kwargs):
    for days in (7, 30, 180, 365):
        base = f"fixed_lookahead_{days}d"
        for name, version in ((base, 1), (base + "_entry_backfilled", 2)):
            connection.execute(EvaluationMethod.__table__.insert().prefix_with("OR IGNORE"), {
                "method_name": name, "family": "fixed_lookahead",
                "method_version": version, "is_active": 1, "scoring_contract": base,
            })


@pytest.fixture(autouse=True)
def seeded_registry():
    event.listen(Base.metadata, "after_create", _seed_scorecard_methods)
    try:
        yield
    finally:
        event.remove(Base.metadata, "after_create", _seed_scorecard_methods)


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
                archived=1,
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
            else:
                # A later-written checkpoint must not appear as the six-month
                # recommendation result just because its method version is newer.
                db.add(PredictionOutcome(
                    prediction_id=prediction.id, outcome_kind="hit_target",
                    pnl_pct=0.90, evaluated_at=event_at + timedelta(days=190),
                    evaluation_method="fixed_lookahead_30d_entry_backfilled",
                ))
        db.commit()

        payload = build_recommendation_scorecard(db, user_id="ariel")

    assert payload["horizons"]["30d"]["wins"] == 1
    assert payload["history_scope"] == "all_saved_recommendations_including_retention_archives"
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
            suggested_payload=json.dumps({
                "order_sheet": {
                    "lines": [{
                        "symbol": "QURE",
                        "action": "BUY",
                        "notional_usd": 10_000,
                    }],
                },
            }),
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
        outcome = PredictionOutcome(
            prediction_id=prediction.id,
            outcome_kind="expired_positive",
            pnl_pct=0.40,
            evaluated_at=event_at + timedelta(days=180),
            evaluation_method="fixed_lookahead_180d",
        )
        db.add(outcome)
        db.flush()
        db.add(PredictionBenchmarkOutcome(
            prediction_outcome_id=outcome.id,
            benchmark_symbol="SPY",
            benchmark_version="spy_adjusted_eod_previous_close_v1",
            comparison_mode="own_vs_benchmark",
            benchmark_start_date=event_at.date(),
            benchmark_end_date=(event_at + timedelta(days=180)).date(),
            benchmark_entry_price=100,
            benchmark_exit_price=115,
            benchmark_return_pct=0.15,
            subject_return_pct=0.40,
            decision_excess_return_pct=0.25,
            evidence_json="{}",
        ))
        db.commit()

        payload = build_recommendation_scorecard(db, user_id="ariel")

    surfaced = payload["surfaced_order_sheets"]
    assert surfaced["horizons"]["180d"]["wins"] == 1
    assert surfaced["recent"][0]["disposition"] == "declined"
    assert surfaced["recent"][0]["ticker"] == "QURE"
    assert payload["shadow_order_sheets"]["evaluated"] == 1
    assert payload["shadow_order_sheets"]["recent"][0]["allocated_usd"] == 10_000
    assert payload["shadow_order_sheets"]["recent"][0]["weighted_excess_return_pct"] == 25.0
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


def test_discovery_call_has_dated_target_and_automatic_report() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    event_at = datetime(2026, 8, 30, tzinfo=UTC)
    with Session(engine) as db:
        db.add(User(id="ariel"))
        pending = Prediction(
            user_id="ariel",
            source=DISCOVERY_EVALUATION_SOURCE,
            source_ref=json.dumps({
                "kind": "discovery_evaluation",
                "verdict": "BUY",
                "conviction": "MED",
                "expectation": "Phase 3 program advances.",
                "success_measure": "Positive return and thesis progress.",
                "horizon_days": 365,
            }),
            ticker="CMPS",
            direction="long",
            entry_price=13.73,
            timeframe_days=365,
            message_id="cmps-self-eval-365",
            event_at=event_at,
            evaluation_due_at=event_at + timedelta(days=365),
            evaluation_method="fixed_lookahead_365d",
        )
        missed = Prediction(
            user_id="ariel",
            source=DISCOVERY_EVALUATION_SOURCE,
            source_ref=json.dumps({
                "kind": "discovery_evaluation",
                "verdict": "PASS",
                "conviction": "LOW",
                "expectation": "Did not clear the sleeve hurdle.",
                "horizon_days": 30,
            }),
            ticker="MRVL",
            direction="long",
            entry_price=80,
            timeframe_days=30,
            message_id="mrvl-self-eval-30",
            event_at=event_at,
            evaluation_due_at=event_at + timedelta(days=30),
            evaluation_method="fixed_lookahead_30d",
        )
        db.add_all([pending, missed])
        db.flush()
        outcome = PredictionOutcome(
            prediction_id=missed.id,
            outcome_kind="hit_target",
            pnl_pct=0.25,
            evaluated_at=event_at + timedelta(days=30),
            evaluation_method="fixed_lookahead_30d",
            entry_price_used=80,
            exit_price_used=100,
        )
        db.add(outcome)
        db.flush()
        db.add(PredictionBenchmarkOutcome(
            prediction_outcome_id=outcome.id,
            benchmark_symbol="SPY",
            benchmark_version="spy_adjusted_eod_previous_close_v1",
            comparison_mode="avoid_vs_benchmark",
            benchmark_start_date=event_at.date(),
            benchmark_end_date=(event_at + timedelta(days=30)).date(),
            benchmark_entry_price=100,
            benchmark_exit_price=110,
            benchmark_return_pct=0.10,
            subject_return_pct=0.25,
            decision_excess_return_pct=-0.15,
            evidence_json="{}",
        ))
        db.commit()

        payload = build_recommendation_scorecard(db, user_id="ariel")

    rows = {row["ticker"]: row for row in payload["self_evaluations"]}
    assert rows["CMPS"]["status"] == "pending"
    assert rows["CMPS"]["evaluation_date"].startswith("2027-08-30")
    assert rows["CMPS"]["target"] == "Positive return and thesis progress."
    assert "automatically" in rows["CMPS"]["report"]
    assert rows["MRVL"]["grade"] == "missed_opportunity"
    assert "$80.00 to $100.00" in rows["MRVL"]["report"]
    assert rows["MRVL"]["benchmark_grade"] == "lag"
    assert rows["MRVL"]["decision_excess_return_pct"] == -15.0
    assert "SPY +10.0%" in rows["MRVL"]["report"]
    assert payload["benchmark"]["lags"] == 1
    engine.dispose()


def test_discovery_fleet_bridge_creates_three_idempotent_clocks() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    event_at = datetime(2026, 8, 30, tzinfo=UTC)
    with Session(engine) as db:
        db.add(User(id="ariel"))
        db.add(ScanState(
            user_id="ariel",
            ticker="ABCL",
            status="active",
            rank=14,
            last_score=80.1,
            radar_fingerprint="abcl-fingerprint",
            estimator_json=json.dumps({
                "ticker": "ABCL", "go": True, "conviction": "MED",
                "sentiment": 0.3, "one_line": "Cap math can work.",
            }),
            fleet_json=json.dumps({
                "ticker": "ABCL", "verdict": "BUY", "conviction": "MED",
                "thesis_md": "Positive Phase 2 with funded runway.", "cites": [],
            }),
            last_fleet_at=event_at,
        ))
        db.add(Prediction(
            user_id="ariel",
            source="signal_stream:radar_observation",
            source_ref="{}",
            ticker="ABCL",
            direction="long",
            entry_price=12.50,
            timeframe_days=30,
            message_id="abcl-radar-30",
            event_at=event_at,
            evaluation_due_at=event_at + timedelta(days=30),
            evaluation_method="fixed_lookahead_30d",
        ))
        db.commit()

        first = ensure_discovery_evaluation_predictions(db, user_id="ariel")
        db.commit()
        second = ensure_discovery_evaluation_predictions(db, user_id="ariel")
        db.commit()
        clocks = db.query(Prediction).filter_by(
            source=DISCOVERY_EVALUATION_SOURCE,
            ticker="ABCL",
        ).order_by(Prediction.timeframe_days).all()

    assert first["predictions_created"] == 3
    assert second["predictions_created"] == 0
    assert [row.timeframe_days for row in clocks] == [30, 180, 365]
    assert all(float(row.entry_price) == 12.50 for row in clocks)
    engine.dispose()


def test_discovery_fleet_bridge_refuses_stale_quote_anchor() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    event_at = datetime(2026, 8, 30, tzinfo=UTC)
    with Session(engine) as db:
        db.add(User(id="ariel"))
        db.add(ScanState(
            user_id="ariel",
            ticker="STALE",
            status="active",
            radar_fingerprint="stale-fingerprint",
            fleet_json=json.dumps({
                "ticker": "STALE", "verdict": "BUY", "conviction": "MED",
                "thesis_md": "A dated thesis.", "cites": [],
            }),
            last_fleet_at=event_at,
        ))
        db.add(Prediction(
            user_id="ariel",
            source="signal_stream:radar_observation",
            source_ref="{}",
            ticker="STALE",
            direction="long",
            entry_price=10,
            timeframe_days=30,
            message_id="stale-radar-30",
            event_at=event_at - timedelta(days=4),
            evaluation_due_at=event_at + timedelta(days=26),
            evaluation_method="fixed_lookahead_30d",
        ))
        db.commit()

        summary = ensure_discovery_evaluation_predictions(db, user_id="ariel")
        db.commit()
        clocks = db.query(Prediction).filter_by(
            source=DISCOVERY_EVALUATION_SOURCE,
            ticker="STALE",
        ).count()

    assert summary["skipped_no_price"] == 1
    assert clocks == 0
    engine.dispose()
