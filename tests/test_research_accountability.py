from datetime import UTC, datetime, timedelta
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from argosy.services.ingest_recommendation_router import route_ingest_recommendations
from argosy.services.recommendation_scorecard import build_recommendation_scorecard
from argosy.services.predictions.benchmark import _comparison_mode
from argosy.services.predictions.writers import write_deep_decision_verdict_predictions
from argosy.state.models import Base, Prediction, User, EvaluationMethod


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="test"))
        for days in (30, 180, 365):
            name = f"fixed_lookahead_{days}d"
            session.add(EvaluationMethod(method_name=name, family="fixed_lookahead", method_version=1,
                                         is_active=1, scoring_contract=name))
        session.commit()
        yield session
    engine.dispose()


def test_pass_has_immutable_clocks_and_is_visible_without_trade(db):
    at = datetime(2026, 9, 25, 10, tzinfo=UTC)
    rec = dict(ticker="XYZ", disposition="pass", rationale="Opportunity cost", next_step="Review earnings", confidence="MEDIUM")
    args = dict(user_id="test", source_kind="research", source_id="post1", source_url="https://example.org",
                recommendations=[rec], assessment_id="assessment1")
    route_ingest_recommendations(db, **args, now=at)
    route_ingest_recommendations(db, **args, now=at+timedelta(days=1))
    rows = db.scalars(select(Prediction)).all()
    assert len(rows) == 3
    assert {r.timeframe_days for r in rows} == {30, 180, 365}
    for row in rows:
        assert row.event_at.replace(tzinfo=UTC) == at
        assert row.evaluation_due_at.replace(tzinfo=UTC) == at+timedelta(days=row.timeframe_days)
        ref = json.loads(row.source_ref)
        assert ref["expectation"] == "Opportunity cost" and not ref["executable"]
        assert _comparison_mode(row) == "avoid_vs_benchmark"
    scorecard = build_recommendation_scorecard(db, user_id="test")
    assert len(scorecard["self_evaluations"]) == 3
    assert all(r["source"] == "research_review" for r in scorecard["self_evaluations"])
    rec["rationale"] = "New primary evidence changes the assessment"
    route_ingest_recommendations(db, **args, now=at+timedelta(days=2))
    assert len(db.scalars(select(Prediction)).all()) == 3  # delivery retry cannot rewrite the assessment
    args["assessment_id"] = "assessment2"
    route_ingest_recommendations(db, **args, now=at+timedelta(days=2))
    assert len(db.scalars(select(Prediction)).all()) == 6  # old version not overwritten


def test_unowned_hold_is_not_scored_as_successful_ownership(db):
    rows = write_deep_decision_verdict_predictions(db, "test", verdict_id=1,
        subject="XYZ", verdict="HOLD", recommendation_intent="do_not_initiate",
        event_at=datetime(2026, 9, 25, tzinfo=UTC))
    assert len(rows) == 3
    assert all(_comparison_mode(r) == "avoid_vs_benchmark" for r in rows)
    assert all(json.loads(r.source_ref)["verdict"] == "HOLD" for r in rows)


def test_unknown_ownership_is_not_a_win_and_bad_intent_cannot_invert_buy(db):
    at = datetime(2026, 9, 25, tzinfo=UTC)
    holds = write_deep_decision_verdict_predictions(db, "test", verdict_id=2,
        subject="XYZ", verdict="HOLD", event_at=at)
    assert all(_comparison_mode(row) == "unresolved_ownership" for row in holds)
    buys = write_deep_decision_verdict_predictions(db, "test", verdict_id=3,
        subject="XYZ", verdict="BUY", recommendation_intent="do_not_initiate", event_at=at)
    assert all(_comparison_mode(row) == "own_vs_benchmark" for row in buys)


def test_removed_candidate_notes_are_withdrawn_but_original_clocks_survive(db):
    from argosy.state.models import ActionProposal
    args = dict(user_id="test", source_kind="research", source_id="withdrawal", source_url="",
                complete_assessment=True, now=datetime(2026, 9, 25, tzinfo=UTC))
    route_ingest_recommendations(db, **args, assessment_id="first", recommendations=[
        dict(ticker="XYZ", disposition="buy", rationale="Original rationale")])
    first = db.scalar(select(ActionProposal))
    assert first.status == "open"
    route_ingest_recommendations(db, **args, assessment_id="second", recommendations=[])
    assert first.status == "superseded"
    assert len(db.scalars(select(Prediction)).all()) == 3
    route_ingest_recommendations(db, **args, assessment_id="third", recommendations=[
        dict(ticker="XYZ", disposition="buy", rationale="New evidence")])
    route_ingest_recommendations(db, **args, assessment_id="third", recommendations=[
        dict(ticker="XYZ", disposition="buy", rationale="New evidence")])
    assert len(db.scalars(select(ActionProposal).where(ActionProposal.status == "open")).all()) == 1
    assert len(db.scalars(select(Prediction)).all()) == 6


@pytest.mark.parametrize("days", [30, 180])
def test_pass_future_outcome_compares_missed_upside_without_claiming_portfolio_profit(db, days):
    from datetime import date
    from argosy.services.predictions.benchmark import ensure_prediction_benchmark_outcomes
    from argosy.services.predictions.evaluator import Bar, run_evaluator_batch
    route_ingest_recommendations(db, user_id="test", source_kind="research", source_id="post2",
        source_url="", assessment_id="assessment-future", now=datetime(2026, 1, 5, tzinfo=UTC),
        recommendations=[dict(ticker="XYZ", disposition="pass", rationale="weak evidence")])
    prediction = db.scalar(select(Prediction).where(Prediction.timeframe_days == days))
    prediction.entry_price = 100  # synthetic historical price; never touches the live DB
    db.flush()
    end = (datetime(2026, 1, 5) + timedelta(days=days)).date()
    run_evaluator_batch(db, now=prediction.evaluation_due_at.replace(tzinfo=UTC),
        price_fetcher=lambda *args: [Bar(date(2026, 1, 5),100,100,100,100), Bar(end,120,120,120,120)])
    result = ensure_prediction_benchmark_outcomes(db, user_id="test", price_fetcher=lambda *args: [
        Bar(date(2026, 1, 2),100,100,100,100), Bar(end,105,105,105,105)])
    assert result.compared == 1
    score = build_recommendation_scorecard(db, user_id="test")
    completed = next(r for r in score["self_evaluations"] if r["prediction_id"] == prediction.id)
    assert completed["grade"] == "missed_opportunity"
    assert completed["source"] == "research_review"
    assert score["benchmark"]["compared"] == 0
