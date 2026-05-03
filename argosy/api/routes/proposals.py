"""Proposals API (SDD §11.1 screen 4, Phase 3).

Endpoints:
  GET  /api/proposals?user_id=&status=
  GET  /api/proposals/{id}
  POST /api/proposals/{id}/approve
  POST /api/proposals/{id}/reject
  POST /api/proposals/{id}/escalate-tier

Each route is multi-tenant: `user_id` is required (query param on GET,
JSON body on POST). Phase 3 has no auth; Phase 5 productization gates
auth and adds the 2nd-factor flow on T3 approves.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from argosy.api.events import publish_event
from argosy.decisions.proposals import (
    IllegalTransitionError,
    ProposalStatus,
    assert_legal,
)
from argosy.decisions.tiers import Tier
from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport as AgentReportRow,
    Approval,
    DecisionRun,
    Proposal as ProposalRow,
    ProposalHistory,
)


router = APIRouter(prefix="/proposals", tags=["proposals"])


# ----------------------------------------------------------------------
# DTOs
# ----------------------------------------------------------------------


class ProposalListItem(BaseModel):
    id: int
    user_id: str
    ticker: str
    action: str
    size_shares_or_currency: float
    size_units: str
    instrument: str
    order_type: str
    tier: str
    account_class: str
    status: str
    rationale_summary: str
    confidence: str | None
    cooling_off_until: str | None
    created_at: str
    updated_at: str


class ProposalListResponse(BaseModel):
    rows: list[ProposalListItem]
    total: int


class ReasoningTrailItem(BaseModel):
    id: int
    agent_role: str
    model: str
    confidence: str | None
    response_text: str
    created_at: str


class ProposalDetailDTO(BaseModel):
    proposal: ProposalListItem
    expected_impact: dict[str, Any] | None
    history: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    reasoning_trail: list[ReasoningTrailItem]
    decision_run: dict[str, Any] | None


class ApproveRequest(BaseModel):
    user_id: str = "ariel"
    channel: Literal["dashboard", "email", "cli"] = "dashboard"
    second_factor: bool = False
    signed_token_id: str | None = None


class RejectRequest(BaseModel):
    user_id: str = "ariel"
    note: str = ""


class EscalateRequest(BaseModel):
    user_id: str = "ariel"
    levels: int = 1


class ProposalActionResponse(BaseModel):
    status: str
    proposal_id: int
    message: str = ""


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _row_to_item(row: ProposalRow) -> ProposalListItem:
    return ProposalListItem(
        id=row.id,
        user_id=row.user_id,
        ticker=row.ticker,
        action=row.action,
        size_shares_or_currency=float(row.size_shares_or_currency),
        size_units=row.size_units,
        instrument=row.instrument,
        order_type=row.order_type,
        tier=row.tier,
        account_class=row.account_class,
        status=row.status,
        rationale_summary=row.rationale_summary,
        confidence=row.confidence,
        cooling_off_until=row.cooling_off_until.isoformat() if row.cooling_off_until else None,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


@router.get("", response_model=ProposalListResponse)
async def list_proposals(
    user_id: str = Query("ariel"),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ProposalListResponse:
    async with db_mod.get_session() as session:
        from sqlalchemy import func

        count_stmt = select(func.count(ProposalRow.id)).where(
            ProposalRow.user_id == user_id
        )
        if status:
            count_stmt = count_stmt.where(ProposalRow.status == status)
        total = (await session.execute(count_stmt)).scalar_one()

        stmt = select(ProposalRow).where(ProposalRow.user_id == user_id)
        if status:
            stmt = stmt.where(ProposalRow.status == status)
        stmt = stmt.order_by(ProposalRow.created_at.desc()).limit(limit).offset(offset)
        rows = (await session.execute(stmt)).scalars().all()
        return ProposalListResponse(
            rows=[_row_to_item(r) for r in rows], total=int(total)
        )


@router.get("/{proposal_id}", response_model=ProposalDetailDTO)
async def get_proposal(
    proposal_id: int,
    user_id: str = Query("ariel"),
) -> ProposalDetailDTO:
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, proposal_id)
        if row is None or row.user_id != user_id:
            raise HTTPException(status_code=404, detail="proposal not found")

        history_rows = (
            await session.execute(
                select(ProposalHistory)
                .where(ProposalHistory.proposal_id == proposal_id)
                .order_by(ProposalHistory.transitioned_at.asc())
            )
        ).scalars().all()
        approval_rows = (
            await session.execute(
                select(Approval).where(Approval.proposal_id == proposal_id)
            )
        ).scalars().all()

        run: DecisionRun | None = None
        trail: list[AgentReportRow] = []
        if row.decision_run_id is not None:
            run = await session.get(DecisionRun, row.decision_run_id)
            trail = (
                await session.execute(
                    select(AgentReportRow)
                    .where(AgentReportRow.decision_id == str(row.decision_run_id))
                    .order_by(AgentReportRow.created_at.asc())
                )
            ).scalars().all()

        try:
            expected = json.loads(row.expected_impact_json) if row.expected_impact_json else None
        except Exception:
            expected = None

        return ProposalDetailDTO(
            proposal=_row_to_item(row),
            expected_impact=expected,
            history=[
                {
                    "status": h.status,
                    "transitioned_at": h.transitioned_at.isoformat(),
                    "transitioned_by": h.transitioned_by,
                    "note": h.note,
                }
                for h in history_rows
            ],
            approvals=[
                {
                    "id": a.id,
                    "approved_at": a.approved_at.isoformat(),
                    "approval_channel": a.approval_channel,
                    "second_factor_used": a.second_factor_used,
                }
                for a in approval_rows
            ],
            reasoning_trail=[
                ReasoningTrailItem(
                    id=t.id,
                    agent_role=t.agent_role,
                    model=t.model,
                    confidence=t.confidence,
                    response_text=t.response_text,
                    created_at=t.created_at.isoformat(),
                )
                for t in trail
            ],
            decision_run=(
                None
                if run is None
                else {
                    "id": run.id,
                    "ticker": run.ticker,
                    "tier": run.tier,
                    "started_at": run.started_at.isoformat(),
                    "finished_at": (
                        run.finished_at.isoformat() if run.finished_at else None
                    ),
                    "status": run.status,
                    "fund_manager_decision": run.fund_manager_decision,
                }
            ),
        )


@router.post("/{proposal_id}/approve", response_model=ProposalActionResponse)
async def approve_proposal(
    proposal_id: int,
    body: ApproveRequest,
) -> ProposalActionResponse:
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, proposal_id)
        if row is None or row.user_id != body.user_id:
            raise HTTPException(status_code=404, detail="proposal not found")

        # T3 requires 2nd-factor (Phase 5 wires it; Phase 3 stub: respect flag).
        if row.tier == "T3" and not body.second_factor:
            raise HTTPException(
                status_code=400,
                detail="T3 approval requires second_factor (Phase 3 stub).",
            )

        src = ProposalStatus(row.status)
        try:
            assert_legal(src, ProposalStatus.APPROVED)
        except IllegalTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        now = _utcnow()
        row.status = ProposalStatus.APPROVED.value
        row.updated_at = now
        session.add(
            ProposalHistory(
                proposal_id=row.id,
                status=row.status,
                transitioned_at=now,
                transitioned_by=f"user:{body.user_id}",
                note=f"Approved via {body.channel}",
            )
        )
        session.add(
            Approval(
                proposal_id=row.id,
                user_id=body.user_id,
                approved_at=now,
                approval_channel=body.channel,
                second_factor_used=body.second_factor,
                signed_token_id=body.signed_token_id,
            )
        )
        await session.commit()

        try:
            await publish_event(
                "proposal.updated",
                {"proposal_id": row.id, "user_id": row.user_id, "status": row.status},
            )
        except Exception:  # pragma: no cover - defensive
            pass

        return ProposalActionResponse(
            status="ok",
            proposal_id=row.id,
            message=f"Proposal #{row.id} approved",
        )


@router.post("/{proposal_id}/reject", response_model=ProposalActionResponse)
async def reject_proposal(
    proposal_id: int,
    body: RejectRequest,
) -> ProposalActionResponse:
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, proposal_id)
        if row is None or row.user_id != body.user_id:
            raise HTTPException(status_code=404, detail="proposal not found")

        src = ProposalStatus(row.status)
        try:
            assert_legal(src, ProposalStatus.REJECTED)
        except IllegalTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        now = _utcnow()
        row.status = ProposalStatus.REJECTED.value
        row.updated_at = now
        session.add(
            ProposalHistory(
                proposal_id=row.id,
                status=row.status,
                transitioned_at=now,
                transitioned_by=f"user:{body.user_id}",
                note=body.note or "Rejected by user",
            )
        )
        await session.commit()

        try:
            await publish_event(
                "proposal.updated",
                {"proposal_id": row.id, "user_id": row.user_id, "status": row.status},
            )
        except Exception:  # pragma: no cover - defensive
            pass

        return ProposalActionResponse(
            status="ok",
            proposal_id=row.id,
            message=f"Proposal #{row.id} rejected",
        )


@router.post("/{proposal_id}/escalate-tier", response_model=ProposalActionResponse)
async def escalate_tier(
    proposal_id: int,
    body: EscalateRequest,
) -> ProposalActionResponse:
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, proposal_id)
        if row is None or row.user_id != body.user_id:
            raise HTTPException(status_code=404, detail="proposal not found")

        try:
            current = Tier.from_str(row.tier)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        new_tier = current.bump_up(max(1, body.levels))
        if new_tier == current:
            return ProposalActionResponse(
                status="noop",
                proposal_id=row.id,
                message=f"Already at top tier {current.value}",
            )

        now = _utcnow()
        row.tier = new_tier.value
        row.updated_at = now
        # Status note (don't auto-cancel; the flow re-runner will pick it up).
        session.add(
            ProposalHistory(
                proposal_id=row.id,
                status=row.status,
                transitioned_at=now,
                transitioned_by=f"user:{body.user_id}",
                note=f"Escalated tier {current.value} -> {new_tier.value}",
            )
        )
        await session.commit()

        try:
            await publish_event(
                "proposal.updated",
                {
                    "proposal_id": row.id,
                    "user_id": row.user_id,
                    "tier": new_tier.value,
                    "status": row.status,
                },
            )
        except Exception:  # pragma: no cover
            pass

        return ProposalActionResponse(
            status="ok",
            proposal_id=row.id,
            message=f"Tier escalated {current.value} -> {new_tier.value}",
        )


__all__ = ["router"]
