"""Real SQLite writers/read model: supersession and rejected-proposal coverage."""
import json
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from argosy.services.predictions.proposal_clocks import (
    PROPOSAL_CLOCK_SOURCE,
    ensure_proposal_prediction_horizons,
)
from argosy.services.predictions.writers import (
    ensure_deep_verdict_prediction_horizons,
    write_deep_decision_verdict_prediction,
)
from argosy.services.recommendation_scorecard import build_recommendation_scorecard
from argosy.services.verdict_registry import write_verdict
from argosy.state.models import Base, Prediction, PredictionOutcome, Proposal, User, Verdict


def test_superseded_call_keeps_all_clocks_and_exact_entry_identity():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="ariel"))
        original = write_verdict(db, user_id="ariel", subject="OLD", verdict="BUY", conviction="MED")
        original.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        write_verdict(db, user_id="ariel", subject="OLD", verdict="HOLD", conviction="MED")
        # ID 1 vs 10 reproduces the old LIKE-prefix join contaminating entry.
        write_deep_decision_verdict_prediction(db, "ariel", verdict_id=10,
            subject="OTHER", verdict="BUY", event_at=original.created_at, entry_price=999)
        db.add(Verdict(user_id="ariel", subject="DRAFT", verdict="BUY", conviction="LOW", settled=False))
        db.commit()
        assert original.settled is False and original.superseded_by is not None
        first = ensure_deep_verdict_prediction_horizons(db, user_id="ariel")
        db.commit()
        second = ensure_deep_verdict_prediction_horizons(db, user_id="ariel")
        assert first["superseded_verdicts"] == 1
        assert second["predictions_created"] == 0
        rows = list(db.scalars(select(Prediction).where(Prediction.ticker == "OLD")))
        old = [row for row in rows if json.loads(row.source_ref)["verdict_id"] == original.id]
        assert sorted(row.timeframe_days for row in old) == [30, 180, 365]
        assert all(row.entry_price is None for row in old)
        assert all(row.event_at.replace(tzinfo=UTC) == datetime(2026, 1, 1, tzinfo=UTC) for row in old)
        assert not list(db.scalars(select(Prediction).where(Prediction.ticker == "DRAFT")))
    engine.dispose()


def test_legacy_rejected_proposals_are_clocked_without_fabricating_verdicts():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    event = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as db:
        db.add_all([User(id="ariel"), User(id="other")])
        for ticker, action, status, run in [("OLD", "buy", "rejected", None),
                                           ("SOLD", "sell", "executed_live", 9),
                                           ("LINKED", "buy", "cancelled", 10)]:
            db.add(Proposal(user_id="ariel", ticker=ticker, action=action, status=status,
                decision_run_id=run, tier="T2", source="decision_funnel",
                created_at=event, limit_price=123, rationale_summary="Original reasoning"))
        # Neither a different user's verdict nor a different ticker is a link.
        db.add_all([
            Verdict(user_id="other", subject="SOLD", verdict="SELL", conviction="MED", source_decision_run_id=9),
            Verdict(user_id="ariel", subject="NOT_SOLD", verdict="SELL", conviction="MED", source_decision_run_id=9),
            Verdict(user_id="ariel", subject="LINKED", verdict="BUY", conviction="MED", source_decision_run_id=10),
        ])
        db.commit()
        before = len(list(db.scalars(select(Verdict))))
        first = ensure_proposal_prediction_horizons(db, user_id="ariel")
        db.commit()
        second = ensure_proposal_prediction_horizons(db, user_id="ariel")
        db.commit()
        assert first == {"eligible_proposals": 2, "predictions_created": 6}
        assert second["predictions_created"] == 0
        assert len(list(db.scalars(select(Verdict)))) == before
        rows = list(db.scalars(select(Prediction).where(Prediction.source == PROPOSAL_CLOCK_SOURCE)))
        assert {row.ticker for row in rows} == {"OLD", "SOLD"}
        assert all(row.entry_price is None and row.target_price is None for row in rows)
        assert all(row.event_at.replace(tzinfo=UTC) == event for row in rows)
        assert {row.direction for row in rows if row.ticker == "SOLD"} == {"short"}
        payload = build_recommendation_scorecard(db, user_id="ariel", recent_limit=1)
        assert payload["coverage"]["legacy_proposals_with_all_clocks"] == 2
        assert payload["coverage"]["legacy_proposals_missing_clocks"] == []
        legacy = payload["legacy_proposal_evaluations"]
        assert len(legacy) == 2  # Not lost behind recent discovery's display limit.
        assert {row["disposition"] for row in legacy} == {"executed", "declined"}
        assert all(row["expectation"] == "Original reasoning" for row in legacy)
        # Retention removes records from scheduling, never from accountability.
        for row in rows:
            row.archived = 1
        db.commit()
        archived_payload = build_recommendation_scorecard(db, user_id="ariel")
        assert archived_payload["coverage"]["legacy_proposals_with_all_clocks"] == 2
        assert len(archived_payload["legacy_proposal_evaluations"]) == 2
        db.add(PredictionOutcome(prediction_id=rows[0].id,
            evaluation_method=rows[0].evaluation_method,
            outcome_kind="unparseable", notes="Provider symbol could not be resolved"))
        db.commit()
        missing = build_recommendation_scorecard(db, user_id="ariel", recent_limit=1)
        assert missing["coverage"]["unscorable_evaluations"] == 1
        assert missing["self_evaluations"][0]["status"] == "unscorable"
        assert missing["self_evaluations"][0]["report"].startswith("Not scored:")
        assert "not a win or a loss" in missing["self_evaluations"][0]["report"]
    engine.dispose()
