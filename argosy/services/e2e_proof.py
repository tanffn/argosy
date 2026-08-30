"""Read-only projection of Argosy's complete order-sheet lifecycle.

The projection deliberately joins existing durable records by the canonical
order-sheet fingerprint.  It does not create a second workflow or infer that a
stage completed merely because an unrelated row exists.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import distinct, select
from sqlalchemy.orm import Session

from argosy.services.order_sheet import OrderSheet, validate_order_sheet
from argosy.services.order_sheet_materializer import order_sheet_fingerprint
from argosy.state.models import (
    ActionProposal,
    AgentReport,
    Fill,
    Lot,
    PendingOrder,
    Prediction,
    PredictionOutcome,
    Proposal,
)


def _json_dict(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _latest_directive(db: Session, user_id: str) -> ActionProposal | None:
    return db.execute(
        select(ActionProposal)
        .where(
            ActionProposal.user_id == user_id,
            ActionProposal.dedup_key == f"period_directive:{user_id}",
        )
        .order_by(ActionProposal.surfaced_at.desc(), ActionProposal.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _known_accounts(db: Session, user_id: str) -> list[str]:
    values = set(
        db.execute(
            select(distinct(Lot.account_id)).where(
                Lot.user_id == user_id,
                Lot.account_id != "",
            )
        ).scalars().all()
    )
    values.update(
        db.execute(
            select(distinct(Proposal.account_id)).where(
                Proposal.user_id == user_id,
                Proposal.account_id != "",
            )
        ).scalars().all()
    )
    return sorted(str(value) for value in values if str(value).strip())


def _empty(user_id: str, accounts: list[str]) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "stage": "not_run",
        "headline": "No validated Argosy run is available yet",
        "known_accounts": accounts,
        "artifact": None,
        "checks": [],
        "lines": [],
        "no_action": [],
        "self_audit": ["Run Argosy to create the first persisted proof chain."],
    }


def build_e2e_proof(db: Session, user_id: str) -> dict[str, Any]:
    """Return the latest real run and every downstream record linked to it."""

    accounts = _known_accounts(db, user_id)
    directive = _latest_directive(db, user_id)
    if directive is None:
        return _empty(user_id, accounts)

    payload = _json_dict(directive.suggested_payload)
    raw_sheet = payload.get("order_sheet")
    if (
        payload.get("artifact_type") != "validated_order_sheet"
        or not isinstance(raw_sheet, dict)
    ):
        result = _empty(user_id, accounts)
        result.update(
            {
                "stage": "not_run",
                "headline": "The latest legacy directive is not an E2E order sheet",
                "self_audit": [
                    f"Directive #{directive.id} predates the validated order-sheet contract."
                ],
            }
        )
        return result

    try:
        sheet = OrderSheet.model_validate(raw_sheet)
    except ValueError as exc:
        return {
            **_empty(user_id, accounts),
            "stage": "blocked",
            "headline": "Latest Argosy run is structurally invalid",
            "artifact": {"action_proposal_id": directive.id},
            "self_audit": [str(exc)],
        }

    validation = validate_order_sheet(sheet)
    fingerprint = order_sheet_fingerprint(sheet)
    recorded_fingerprint = str(payload.get("order_sheet_fingerprint") or "")
    team_report_ids = [
        int(value)
        for value in (payload.get("team_agent_report_ids") or [])
        if str(value).isdigit()
    ]
    team_reports = list(
        db.execute(
            select(AgentReport)
            .where(
                AgentReport.user_id == user_id,
                AgentReport.id.in_(team_report_ids or [-1]),
            )
            .order_by(AgentReport.id)
        ).scalars().all()
    )
    proposals = list(
        db.execute(
            select(Proposal)
            .where(
                Proposal.user_id == user_id,
                Proposal.source == "order_sheet",
                Proposal.expected_impact_json.like(f"%{fingerprint}%"),
            )
            .order_by(Proposal.id)
        ).scalars().all()
    )
    proposal_ids = [row.id for row in proposals]
    pending_by_proposal: dict[int, PendingOrder] = {}
    fills_by_proposal: dict[int, list[Fill]] = {pid: [] for pid in proposal_ids}
    if proposal_ids:
        for row in db.execute(
            select(PendingOrder).where(PendingOrder.proposal_id.in_(proposal_ids))
        ).scalars().all():
            pending_by_proposal[row.proposal_id] = row
        for row in db.execute(
            select(Fill)
            .where(Fill.proposal_id.in_(proposal_ids))
            .order_by(Fill.filled_at)
        ).scalars().all():
            if row.proposal_id is not None:
                fills_by_proposal.setdefault(row.proposal_id, []).append(row)

    predictions: list[Prediction] = []
    for row in db.execute(
        select(Prediction).where(
            Prediction.user_id == user_id,
            Prediction.source == "signal_stream:order_sheet",
        )
    ).scalars().all():
        if _json_dict(row.source_ref).get("order_sheet_fingerprint") == fingerprint:
            predictions.append(row)
    outcomes_by_prediction: dict[int, list[PredictionOutcome]] = {
        row.id: [] for row in predictions
    }
    if predictions:
        for outcome in db.execute(
            select(PredictionOutcome).where(
                PredictionOutcome.prediction_id.in_([row.id for row in predictions])
            )
        ).scalars().all():
            outcomes_by_prediction.setdefault(outcome.prediction_id, []).append(outcome)
    prediction_by_proposal = {
        int(_json_dict(row.source_ref).get("proposal_id")): row
        for row in predictions
        if str(_json_dict(row.source_ref).get("proposal_id", "")).isdigit()
    }

    proposals_by_symbol = {row.ticker.upper(): row for row in proposals}
    lines: list[dict[str, Any]] = []
    complete_lines = 0
    any_fill = False
    any_approved = False
    for authored in sheet.lines:
        proposal = proposals_by_symbol.get(authored.symbol)
        fill_rows = fills_by_proposal.get(proposal.id, []) if proposal else []
        filled_qty = sum(float(row.quantity) for row in fill_rows)
        fill_notional = sum(float(row.quantity) * float(row.price) for row in fill_rows)
        vwap = fill_notional / filled_qty if filled_qty else None
        target_qty = float(proposal.size_shares_or_currency) if proposal else authored.shares
        is_complete = bool(proposal and filled_qty + 0.0001 >= target_qty)
        complete_lines += int(is_complete)
        any_fill = any_fill or bool(fill_rows)
        any_approved = any_approved or bool(
            proposal and proposal.status in {"approved", "executed_live", "executed_paper"}
        )
        prediction = prediction_by_proposal.get(proposal.id) if proposal else None
        outcomes = outcomes_by_prediction.get(prediction.id, []) if prediction else []
        pending = pending_by_proposal.get(proposal.id) if proposal else None
        lines.append(
            {
                "authored": authored.model_dump(mode="json"),
                "proposal": (
                    {
                        "id": proposal.id,
                        "status": proposal.status,
                        "account_id": proposal.account_id,
                        "limit_price": float(proposal.limit_price or 0),
                        "target_quantity": target_qty,
                    }
                    if proposal
                    else None
                ),
                "execution": {
                    "pending_status": pending.status if pending else None,
                    "broker": pending.broker if pending else None,
                    "broker_order_id": pending.broker_order_id if pending else None,
                    "filled_quantity": round(filled_qty, 4),
                    "fill_count": len(fill_rows),
                    "vwap": round(vwap, 4) if vwap is not None else None,
                    "commission_usd": round(sum(float(row.commission) for row in fill_rows), 2),
                    "complete": is_complete,
                    "manual_fill_allowed": bool(
                        proposal
                        and proposal.status in {"approved", "executed_live"}
                        and proposal.account_id.lower().startswith(("schwab", "leumi"))
                    ),
                },
                "calibration": (
                    {
                        "prediction_id": prediction.id,
                        "due_at": prediction.evaluation_due_at.isoformat(),
                        "entry_price": float(prediction.entry_price or 0),
                        "status": "scored" if outcomes else "scheduled",
                        "outcomes": [
                            {
                                "kind": outcome.outcome_kind,
                                "pnl_pct": (
                                    float(outcome.pnl_pct)
                                    if outcome.pnl_pct is not None
                                    else None
                                ),
                                "evaluated_at": outcome.evaluated_at.isoformat(),
                            }
                            for outcome in outcomes
                        ],
                    }
                    if prediction
                    else None
                ),
            }
        )

    exact_materialization = len(proposals) == len(sheet.lines) and bool(proposals)
    telemetry_linked = exact_materialization and len(predictions) == len(proposals)
    fresh = _aware(directive.expires_at) >= datetime.now(UTC)
    one_voice_passed = bool(
        sheet.review_resolution is not None
        and sheet.review_resolution.one_voice
        and not any(
            failure.code in {"one_voice_conflict", "team_review_unresolved"}
            for failure in validation.failures
        )
    )
    checks = [
        {"key": "validated", "label": "Schema + arithmetic", "passed": validation.valid},
        {"key": "fingerprint", "label": "Artifact fingerprint", "passed": recorded_fingerprint == fingerprint},
        {"key": "fresh", "label": "Live facts eligible", "passed": fresh},
        {
            "key": "one_voice",
            "label": "Independent reviewers reconciled",
            "passed": one_voice_passed,
        },
        {"key": "coverage", "label": "Portfolio + NO-ACTION coverage", "passed": not any(f.code == "portfolio_coverage_missing" for f in validation.failures)},
        {"key": "team_telemetry", "label": "LLM calls + cost recorded", "passed": bool(team_reports) and len(team_reports) == len(team_report_ids)},
        {"key": "materialized", "label": "Exact orders materialized", "passed": exact_materialization},
        {"key": "telemetry", "label": "Dated outcomes linked", "passed": telemetry_linked},
        {"key": "fills", "label": "Broker fills reconciled", "passed": bool(lines) and complete_lines == len(lines)},
    ]

    audit_failures = [f"{failure.code}: {failure.detail}" for failure in validation.failures]
    if recorded_fingerprint != fingerprint:
        audit_failures.append("Stored fingerprint does not match the order-sheet body.")
    if directive.status == "accepted" and not exact_materialization:
        audit_failures.append("Accepted directive does not have exactly one proposal per line.")
    if exact_materialization and not telemetry_linked:
        audit_failures.append("Executable proposals are missing dated prediction telemetry.")
    if not team_reports or len(team_reports) != len(team_report_ids):
        audit_failures.append("Author/reviewer LLM telemetry is not durably linked to this run.")
    if not one_voice_passed:
        audit_failures.append("Independent reviewer reconciliation is missing or unresolved.")

    if (
        not validation.valid
        or recorded_fingerprint != fingerprint
        or not fresh
        or not one_voice_passed
    ):
        stage = "blocked"
        headline = "Argosy stopped this run before execution"
    elif directive.status == "open":
        stage = "ready_to_accept"
        headline = "Validated order sheet ready for your decision"
    elif not exact_materialization:
        stage = "broken"
        headline = "Accepted sheet failed exact materialization"
    elif complete_lines == len(lines):
        stage = "complete"
        headline = "E2E run complete and awaiting outcome scoring"
    elif any_fill:
        stage = "partially_filled"
        headline = "Broker fills are being reconciled"
    elif any_approved:
        stage = "execution"
        headline = "Approved orders await execution receipts"
    else:
        stage = "awaiting_order_approval"
        headline = "Exact trade lines await your approval"

    return {
        "user_id": user_id,
        "stage": stage,
        "headline": headline,
        "known_accounts": accounts,
        "artifact": {
            "action_proposal_id": directive.id,
            "status": directive.status,
            "execution_state": directive.execution_state,
            "generated_at": sheet.generated_at.isoformat(),
            "surfaced_at": directive.surfaced_at.isoformat(),
            "expires_at": directive.expires_at.isoformat(),
            "fingerprint": fingerprint,
            "funding": sheet.funding.model_dump(mode="json"),
            "rationale": sheet.rationale,
            "horizon_years": [sheet.horizon_years_min, sheet.horizon_years_max],
            "validation": validation.model_dump(mode="json"),
            "team_telemetry": {
                "decision_ids": payload.get("team_decision_ids") or [],
                "total_cost_usd": round(
                    sum(float(report.cost_usd) for report in team_reports), 6
                ),
                "reports": [
                    {
                        "id": report.id,
                        "role": report.agent_role,
                        "model": report.model,
                        "cost_usd": float(report.cost_usd),
                        "created_at": report.created_at.isoformat(),
                    }
                    for report in team_reports
                ],
            },
        },
        "checks": checks,
        "lines": lines,
        "no_action": [row.model_dump(mode="json") for row in sheet.no_action],
        "self_audit": audit_failures,
    }


__all__ = ["build_e2e_proof"]
