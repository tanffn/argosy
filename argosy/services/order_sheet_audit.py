"""Read the execution and outcome feedback for one materialized order sheet."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.state.models import Fill, Prediction, PredictionOutcome, Proposal


class OrderLineAudit(BaseModel):
    symbol: str
    action: str
    proposal_id: int
    proposal_status: str
    account_id: str
    expected_shares: float
    expected_price_usd: float
    filled_shares: float = 0
    average_fill_price_usd: float | None = None
    adverse_slippage_bps: float | None = None
    commission_usd: float = 0
    prediction_id: int | None = None
    expectation_due_at: str | None = None
    outcome_kind: str | None = None
    outcome_pnl_pct: float | None = None


class OrderSheetAudit(BaseModel):
    fingerprint: str
    lines: list[OrderLineAudit] = Field(default_factory=list)
    proposals: int = 0
    proposals_filled: int = 0
    predictions_due: int = 0
    predictions_scored: int = 0


def audit_order_sheet(
    session: Session,
    *,
    user_id: str,
    fingerprint: str,
) -> OrderSheetAudit:
    proposals = list(
        session.execute(
            select(Proposal)
            .where(Proposal.user_id == user_id, Proposal.source == "order_sheet")
            .where(Proposal.expected_impact_json.like(f"%{fingerprint}%"))
            .order_by(Proposal.id)
        )
        .scalars()
        .all()
    )
    prediction_rows = list(
        session.execute(
            select(Prediction).where(
                Prediction.user_id == user_id,
                Prediction.source == "signal_stream:order_sheet",
                Prediction.message_id.like(f"%{fingerprint}.%"),
            )
        )
        .scalars()
        .all()
    )
    prediction_by_proposal: dict[int, Prediction] = {}
    for prediction in prediction_rows:
        try:
            proposal_id = int(json.loads(prediction.source_ref)["proposal_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        prediction_by_proposal[proposal_id] = prediction

    lines: list[OrderLineAudit] = []
    scored = 0
    for proposal in proposals:
        try:
            payload: dict[str, Any] = json.loads(proposal.expected_impact_json)
            order_line = payload["order_line"]
            expected_price = float(order_line["evidence"]["price_usd"])
            expected_shares = float(order_line["shares"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            expected_price = float(proposal.limit_price or 0.0)
            expected_shares = float(proposal.size_shares_or_currency)
        fills = list(
            session.execute(select(Fill).where(Fill.proposal_id == proposal.id))
            .scalars()
            .all()
        )
        filled_shares = sum(float(row.quantity) for row in fills)
        fill_notional = sum(float(row.quantity) * float(row.price) for row in fills)
        average_fill = fill_notional / filled_shares if filled_shares > 0 else None
        adverse_bps = None
        if average_fill is not None and expected_price > 0:
            raw = (average_fill - expected_price) / expected_price * 10_000
            adverse_bps = raw if proposal.action == "buy" else -raw
        prediction = prediction_by_proposal.get(proposal.id)
        outcome = None
        if prediction is not None:
            outcome = session.execute(
                select(PredictionOutcome)
                .where(PredictionOutcome.prediction_id == prediction.id)
                .order_by(PredictionOutcome.evaluated_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if outcome is not None:
            scored += 1
        lines.append(
            OrderLineAudit(
                symbol=proposal.ticker,
                action=proposal.action,
                proposal_id=proposal.id,
                proposal_status=proposal.status,
                account_id=proposal.account_id,
                expected_shares=expected_shares,
                expected_price_usd=expected_price,
                filled_shares=filled_shares,
                average_fill_price_usd=average_fill,
                adverse_slippage_bps=adverse_bps,
                commission_usd=sum(float(row.commission) for row in fills),
                prediction_id=prediction.id if prediction is not None else None,
                expectation_due_at=(
                    prediction.evaluation_due_at.isoformat()
                    if prediction is not None
                    else None
                ),
                outcome_kind=outcome.outcome_kind if outcome is not None else None,
                outcome_pnl_pct=(
                    float(outcome.pnl_pct)
                    if outcome is not None and outcome.pnl_pct is not None
                    else None
                ),
            )
        )
    return OrderSheetAudit(
        fingerprint=fingerprint,
        lines=lines,
        proposals=len(proposals),
        proposals_filled=sum(1 for row in lines if row.filled_shares > 0),
        predictions_due=len(prediction_rows),
        predictions_scored=scored,
    )


__all__ = ["OrderLineAudit", "OrderSheetAudit", "audit_order_sheet"]
