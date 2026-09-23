"""Read the execution and outcome feedback for one materialized order sheet."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.execution.fill_evidence import reconcile_fill_evidence
from argosy.services.predictions.outcomes import authoritative_outcomes
from argosy.state.models import Fill, Prediction, Proposal


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
    commission_usd: float | None = None
    prediction_id: int | None = None
    expectation_due_at: str | None = None
    outcome_kind: str | None = None
    outcome_pnl_pct: float | None = None
    paper_fill_count: int = 0
    receipt_errors: list[str] = Field(default_factory=list)


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
                Prediction.evaluation_method == "order_sheet_due_date_v1",
                Prediction.message_id.like(f"%{fingerprint}.%"),
            )
        )
        .scalars()
        .all()
    )
    prediction_by_line: dict[tuple[str, str], Prediction] = {}
    for prediction in prediction_rows:
        try:
            ref = json.loads(prediction.source_ref)
            if ref["order_sheet_fingerprint"] != fingerprint:
                continue
            action = ref["action"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        prediction_by_line[(prediction.ticker, action)] = prediction

    lines: list[OrderLineAudit] = []
    selected_outcomes = authoritative_outcomes(session, prediction_rows)
    scored = 0
    for proposal in proposals:
        try:
            payload: dict[str, Any] = json.loads(proposal.expected_impact_json)
            if not isinstance(payload, dict) or payload.get("order_sheet_fingerprint") != fingerprint:
                continue
        except (TypeError, ValueError):
            continue
        try:
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
        evidence = reconcile_fill_evidence(proposal, fills, target_quantity=expected_shares)
        filled_shares = float(evidence.quantity)
        average_fill = evidence.vwap if evidence.price_currency == "USD" else None
        adverse_bps = None
        if average_fill is not None and expected_price > 0:
            raw = (average_fill - expected_price) / expected_price * 10_000
            adverse_bps = raw if proposal.action == "buy" else -raw
        try:
            action = payload["order_line"]["action"]
        except (KeyError, TypeError):
            action = None
        prediction = prediction_by_line.get((proposal.ticker, action))
        outcome = None
        if prediction is not None:
            selected = selected_outcomes.get(prediction.id)
            outcome = selected[1] if selected is not None else None
        if outcome is not None and outcome.outcome_kind != "unparseable":
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
                commission_usd=float(evidence.commission) if evidence.commission_confirmed and evidence.commission_currency == "USD" else None,
                paper_fill_count=evidence.paper_count,
                receipt_errors=evidence.errors,
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
        proposals=len(lines),
        proposals_filled=sum(1 for row in lines if row.filled_shares > 0),
        predictions_due=len(prediction_by_line),
        predictions_scored=scored,
    )


__all__ = ["OrderLineAudit", "OrderSheetAudit", "audit_order_sheet"]
