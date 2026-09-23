"""Outcome clocks for durable autonomous proposals predating verdict lineage.

Do not manufacture Verdict rows or use an order's limit as its market entry.
Historical entries are resolved by the existing as-of price evaluator. No
acceptance/execution filter: rejected and cancelled calls remain accountable.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from argosy.services.current_recommendations import AUTONOMOUS_PROPOSAL_SOURCES
from argosy.services.predictions.writers import _insert_prediction
from argosy.state.models import Prediction, Proposal, Verdict

PROPOSAL_CLOCK_SOURCE = "signal_stream:proposal_recommendation"


def proposals_without_verdict(session: Session, *, user_id: str | None = None) -> list[Proposal]:
    """Exact tenant/run/instrument lineage; a different ticker is not a link."""
    linked = select(Verdict.id).where(
        Verdict.user_id == Proposal.user_id,
        Verdict.source_decision_run_id == Proposal.decision_run_id,
        func.upper(Verdict.subject) == func.upper(Proposal.ticker),
    ).exists()
    stmt = select(Proposal).where(
        Proposal.source.in_(AUTONOMOUS_PROPOSAL_SOURCES),
        Proposal.action.in_(("buy", "sell")),
        ~linked,
    )
    if user_id is not None:
        stmt = stmt.where(Proposal.user_id == user_id)
    return list(session.scalars(stmt.order_by(Proposal.id)))


def ensure_proposal_prediction_horizons(
    session: Session, *, user_id: str | None = None,
) -> dict[str, int]:
    """Replay-safe daily repair using original proposal timestamp and rationale."""
    proposals = proposals_without_verdict(session, user_id=user_id)
    existing = set(session.scalars(select(Prediction.message_id).where(
        Prediction.source == PROPOSAL_CLOCK_SOURCE,
        *((Prediction.user_id == user_id,) if user_id is not None else ()),
    )))
    created = 0
    for row in proposals:
        for days in (30, 180, 365):
            message_id = f"v1|proposal_recommendation|{row.id}|{days}d"
            _insert_prediction(
                session, row.user_id, source=PROPOSAL_CLOCK_SOURCE,
                source_ref={
                    "proposal_id": row.id, "decision_run_id": row.decision_run_id,
                    "action": row.action.upper(), "horizon_days": days,
                    "expectation": row.rationale_summary, "conviction": row.confidence,
                    "provenance": "original_proposal_no_verdict",
                },
                message_id=message_id, ticker=row.ticker,
                direction="long" if row.action == "buy" else "short",
                event_at=row.created_at, timeframe_days=days,
                raw_text_ref=f"proposals.id:{row.id}", preserve_long_horizon=True,
            )
            if message_id not in existing:
                created += 1
                existing.add(message_id)
    return {"eligible_proposals": len(proposals), "predictions_created": created}
