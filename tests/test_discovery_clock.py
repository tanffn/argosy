"""Forward-only clock policy; legacy prices/outcomes are never rewritten."""
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from argosy.services.predictions.evaluator import Bar, _compute_backfilled_outcome
from argosy.services.predictions.writers import write_discovery_evaluation_predictions
from argosy.state.models import Base, User


def test_new_discovery_clocks_keep_unavailable_execution_entry_unknown():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id='ariel'))
        db.flush()
        rows = write_discovery_evaluation_predictions(
            db, 'ariel', event_key='real-completion', ticker='XYZ', verdict='WATCH',
            conviction='MED', event_at=datetime(2026, 1, 5, 22, tzinfo=UTC),
            entry_price=None, entry_price_policy='post_decision_bar', estimator_go=True,
            estimator_conviction='MED', estimation='Research', thesis='Dated thesis',
            radar_rank=1, radar_score=80,
            research_context={"evidence_contract": "complete-analyst-fields-v1", "research_contract": "test-v4"},
        )
        assert [row.timeframe_days for row in rows] == [30, 180, 365]
        assert all(row.entry_price is None for row in rows)
        assert all(json.loads(row.source_ref)['entry_price_policy'] == 'post_decision_bar' for row in rows)
        assert all(json.loads(row.source_ref)['research_context'] == {
            'evidence_contract': 'complete-analyst-fields-v1', 'research_contract': 'test-v4'} for row in rows)
    engine.dispose()


def test_post_decision_entry_never_uses_a_prior_or_same_day_close():
    prediction = SimpleNamespace(
        ticker='XYZ', entry_price=None, evaluation_method='fixed_lookahead_30d',
        source_ref=json.dumps({'entry_price_policy': 'post_decision_bar'}),
        event_at=datetime(2026, 1, 5, 23, tzinfo=UTC), timeframe_days=30,
        evaluation_due_at=datetime(2026, 2, 4, 23, tzinfo=UTC), direction='long',
    )
    bars = [Bar(date(2026, 1, 2), 50, 50, 50, 50),
            Bar(date(2026, 1, 5), 70, 70, 70, 70),
            Bar(date(2026, 1, 6), 100, 100, 100, 100),
            Bar(date(2026, 2, 4), 150, 150, 150, 150)]
    outcome = _compute_backfilled_outcome(prediction, price_fetcher=lambda *args: bars)
    assert outcome.entry_price_used == 100
    assert outcome.pnl_pct == pytest.approx(.5)
    assert outcome.evidence['entry_bar']['date'] == '2026-01-06'


@pytest.mark.parametrize('versioned', [False, True])
def test_recovery_keeps_full_falsifiers_and_stored_contracts_without_rewriting_calls(versioned):
    from argosy.services.predictions.writers import ensure_discovery_evaluation_predictions
    from argosy.state.models import Prediction, ScanState

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id='ariel'))
        db.flush()
        thesis = 'Dated hypothesis\n' + 'evidence ' * 800 + '\nFALSIFIER: customer loss. REVIEW: 2026-11-01.'
        fleet = {'verdict': 'WATCH', 'conviction': 'LOW', 'thesis_md': thesis,
                 'entry_price_policy': 'post_decision_bar'}
        if versioned:
            fleet.update(research_contract='recorded-v4', evidence_contract='complete-analyst-fields-v1')
        scan = ScanState(user_id='ariel', ticker='XYZ', fleet_json=json.dumps(fleet),
                         last_fleet_at=datetime(2026, 10, 23, tzinfo=UTC), radar_fingerprint='original')
        db.add(scan)
        db.flush()
        assert ensure_discovery_evaluation_predictions(db, user_id='ariel')['predictions_created'] == 3
        original = [p.source_ref for p in db.scalars(select(Prediction).order_by(Prediction.id))]
        assert all(json.loads(ref)['expectation'] == thesis for ref in original)
        assert all(json.loads(ref)['research_context'] == ({
            'research_contract': 'recorded-v4', 'evidence_contract': 'complete-analyst-fields-v1'} if versioned else {}) for ref in original)
        fleet.update(thesis_md='later wording must not replace original', research_contract='later')
        scan.fleet_json = json.dumps(fleet)
        db.flush()
        assert ensure_discovery_evaluation_predictions(db, user_id='ariel')['predictions_created'] == 0
        assert [p.source_ref for p in db.scalars(select(Prediction).order_by(Prediction.id))] == original
    engine.dispose()


def test_benchmark_uses_exact_subject_post_decision_entry_day():
    from argosy.services.predictions.benchmark import (
        POST_DECISION_BENCHMARK_VERSION,
        ensure_prediction_benchmark_outcomes,
    )
    from argosy.state.models import Prediction, PredictionBenchmarkOutcome, PredictionOutcome
    from tests.test_prediction_benchmark import _seed

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        outcome = _seed(db, verdict='BUY', direction='long', signed_pnl=.5)
        prediction = db.get(Prediction, outcome.prediction_id)
        prediction.source_ref = json.dumps({'verdict': 'BUY', 'entry_price_policy': 'post_decision_bar'})
        prediction.entry_price = None
        outcome.evidence_json = json.dumps({'entry_bar': {'date': '2026-01-06'}})
        outcome.entry_price_used = 100
        outcome.exit_price_used = 150
        bars = [Bar(date(2026, 1, 2), 100, 100, 100, 100),
                Bar(date(2026, 1, 6), 200, 200, 200, 200),
                Bar(date(2026, 2, 4), 220, 220, 220, 220)]
        summary = ensure_prediction_benchmark_outcomes(db, price_fetcher=lambda *args: bars)
        row = db.scalar(select(PredictionBenchmarkOutcome))
        assert summary.compared == 1
        assert row.benchmark_version == POST_DECISION_BENCHMARK_VERSION
        assert row.benchmark_start_date == date(2026, 1, 6)
        assert float(row.benchmark_return_pct) == pytest.approx(.1)
        assert float(row.decision_excess_return_pct) == pytest.approx(.4)
        from argosy.services.predictions.benchmark import benchmark_version_expression
        # The allocation-feedback reader uses this same shared SQL policy.
        selected = db.scalar(select(PredictionBenchmarkOutcome)
                             .join(PredictionOutcome, PredictionOutcome.id == PredictionBenchmarkOutcome.prediction_outcome_id)
                             .join(Prediction, Prediction.id == PredictionOutcome.prediction_id)
                             .where(PredictionBenchmarkOutcome.benchmark_version == benchmark_version_expression()))
        assert selected.id == row.id
        assert ensure_prediction_benchmark_outcomes(db, price_fetcher=lambda *args: bars).compared == 0
    engine.dispose()
