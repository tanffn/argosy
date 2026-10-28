import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from argosy.services.discovery_audit import build_discovery_audit
from argosy.state.models import (
    Base,
    DecisionRun,
    EvaluationMethod,
    Prediction,
    PredictionOutcome,
    ScanState,
    User,
)


def test_audit_counts_first_calls_both_tails_and_unknowns_without_tenant_leak():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 1, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all([User(id="a"), User(id="b"), EvaluationMethod(
            method_name="fixed_lookahead_30d", family="fixed", method_version=1,
        )])
        session.flush()

        def add(ticker, *, verdict=None, day=0, pnl=None, user="a", archived=0):
            p = Prediction(user_id=user, ticker=ticker,
                           source="signal_stream:discovery_evaluation" if verdict else "signal_stream:radar_observation",
                           source_ref=json.dumps({"verdict": verdict, "expectation": "Original thesis"}),
                           direction="long", timeframe_days=30,
                           event_at=now + timedelta(days=day),
                           evaluation_due_at=now + timedelta(days=day + 30),
                           evaluation_method="fixed_lookahead_30d", archived=archived)
            session.add(p)
            session.flush()
            if pnl is not None:
                session.add(PredictionOutcome(prediction_id=p.id, outcome_kind="win" if pnl > 0 else "loss",
                                              pnl_pct=pnl, evaluation_method="fixed_lookahead_30d"))

        add("WIN", verdict="PASS", pnl=.4, archived=1)
        add("WIN", verdict="BUY", day=10, pnl=.9)  # not an independent first-call win
        add("LOSE", verdict="WATCH", pnl=-.5)
        add("UNSEEN_BY_FLEET")
        add("PRIVATE", verdict="BUY", pnl=10, user="b")
        session.commit()
        audit = build_discovery_audit(session, user_id="a")
        assert audit["coverage"]["unique_names"] == 3
        assert audit["coverage"]["scored"] == 2
        assert audit["coverage"]["skipped_then_rose"] == 1
        assert audit["coverage"]["skipped_then_fell"] == 1
        assert audit["missed_upside_examples"][0]["gross_return"] == .4
        assert audit["avoided_decline_examples"][0]["gross_return"] == -.5
        assert all(row["ticker"] != "PRIVATE" for row in audit["rows"])
        annual = build_discovery_audit(session, user_id="a", horizon_days=365)
        assert annual['coverage']['unique_names'] == 3
        assert annual['coverage']['scored'] == 0
        assert {row['outcome_status'] for row in annual['rows']} == {'missing_horizon_clock'}
    engine.dispose()


def test_tracking_and_screen_failure_survive_missing_clocks_and_recover_later():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(User(id='a'))
        session.flush()
        state = ScanState(user_id='a', ticker='OBSERVED', status='active', last_radar_at=now)
        session.add(state)
        session.add(DecisionRun(user_id='a', ticker='OBSERVED', decision_kind='discovery_screen',
                                execution_policy='analysis_only', tier='screen', status='blocked',
                                started_at=now, finished_at=now,
                                notes_json=json.dumps({'blocked_reason': 'Estimator failed: unavailable'})))
        session.commit()
        audit = build_discovery_audit(session, user_id='a')
        assert audit['coverage']['unique_names'] == 1
        assert audit['coverage']['tracked_without_clock'] == 1
        assert audit['rows'][0]['outcome_status'] == 'missing_observation_clock'
        assert audit['research_status']['status'] == 'incomplete'
        assert 'Estimator failed' in audit['research_status']['failures'][0]['reason']
        state.last_estimated_at = now + timedelta(seconds=1)
        session.commit()
        assert build_discovery_audit(session, user_id='a')['research_status']['failures'] == []
    engine.dispose()
