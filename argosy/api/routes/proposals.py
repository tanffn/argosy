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

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from argosy.agent_settings import load_agent_settings
from argosy.api.events import publish_event
from argosy.decisions.proposals import (
    IllegalTransitionError,
    ProposalStatus,
    assert_legal,
)
from argosy.decisions.tiers import Tier
from argosy.security import totp as totp_mod
from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport as AgentReportRow,
    Approval,
    DecisionRun,
    Proposal as ProposalRow,
    ProposalHistory,
    TOTPSecret,
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
    # T4.2: surface speculative-candidate metadata onto the row so the
    # UI can render conviction + citations without a second round-trip.
    # ``conviction`` is an alias of ``confidence`` (HIGH/MEDIUM/LOW) — we
    # keep both for backwards-compat with existing consumers.
    # ``cited_sources`` is pulled from ``expected_impact_json.sourced_from``
    # for speculation-origin proposals; empty list for non-speculative
    # proposals or when the synthesizer did not emit sources.
    conviction: str | None = None
    cited_sources: list[str] = []


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


def _extract_cited_sources(expected_impact_json: str | None) -> list[str]:
    """Pull ``sourced_from`` (a list[str]) out of ``expected_impact_json``.

    Speculation-origin proposals (T4.2) persist ``sourced_from`` from the
    synthesizer's ``SpeculativeCandidate`` into ``expected_impact_json``
    under the ``sourced_from`` key. Non-speculation proposals do not
    populate this key, so we return an empty list. Any parse error or
    unexpected shape is also treated as "no citations".
    """
    if not expected_impact_json:
        return []
    try:
        blob = json.loads(expected_impact_json)
    except Exception:
        return []
    if not isinstance(blob, dict):
        return []
    raw = blob.get("sourced_from")
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if isinstance(x, (str, int, float))]


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
        # T4.2: ``conviction`` mirrors ``confidence`` (the column was named
        # ``confidence`` before the UI/spec adopted the term "conviction").
        conviction=row.confidence,
        cited_sources=_extract_cited_sources(row.expected_impact_json),
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
    x_totp_code: str | None = Header(default=None, alias="X-TOTP-Code"),
) -> ProposalActionResponse:
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, proposal_id)
        if row is None or row.user_id != body.user_id:
            raise HTTPException(status_code=404, detail="proposal not found")

        # ----- T3 second-factor (Phase 5: TOTP or delay) ---------------------
        # Per `agent_settings.security.t3_second_factor`:
        #   - "totp"  → require a valid X-TOTP-Code header
        #   - "delay" → require body.second_factor=True AND first approval
        #               was at least `delay_minutes` ago
        if row.tier == "T3":
            settings = load_agent_settings(body.user_id)
            kind = settings.security.t3_second_factor
            if kind == "totp":
                if not x_totp_code:
                    raise HTTPException(
                        status_code=400,
                        detail="T3 approval requires X-TOTP-Code header.",
                    )
                secret_row = await session.get(TOTPSecret, body.user_id)
                if secret_row is None or not secret_row.secret_encrypted:
                    raise HTTPException(
                        status_code=400,
                        detail="user has no TOTP secret enrolled; "
                        "POST /api/security/totp/setup first.",
                    )
                last_used_counter: int | None = None
                if secret_row.last_verified_at is not None:
                    last_used_counter = int(
                        secret_row.last_verified_at.timestamp()
                        // totp_mod.DEFAULT_STEP_SECONDS
                    )
                try:
                    result = totp_mod.verify_code(
                        secret_row.secret_encrypted,
                        x_totp_code,
                        last_used_counter=last_used_counter,
                    )
                    secret_row.last_verified_at = _utcnow().replace(
                        microsecond=0
                    )
                except totp_mod.TOTPVerificationError as exc:
                    raise HTTPException(
                        status_code=401, detail=f"TOTP failed: {exc}"
                    ) from exc
                # Mark second_factor_used=True regardless of body flag.
                body.second_factor = True
                _ = result  # kept for audit completeness
            elif kind == "delay":
                # Backwards-compat: callers must still set second_factor=True
                # to opt into the delay flow (Phase 3 contract preserved;
                # Phase 5 layers a 1h gap on top).
                if not body.second_factor:
                    raise HTTPException(
                        status_code=400,
                        detail="T3 approval requires second_factor (delay mode).",
                    )
                # Two-step: first call records an Approval pending; the
                # second call (>= delay_minutes later) commits. We use
                # the existing `approvals` table: the first row carries
                # a pending marker via `signed_token_id="pending"`.
                first_pending = (
                    await session.execute(
                        select(Approval)
                        .where(
                            Approval.proposal_id == row.id,
                            Approval.signed_token_id == "pending",
                        )
                        .order_by(Approval.approved_at.asc())
                    )
                ).scalars().first()
                from datetime import timedelta as _td

                if first_pending is None:
                    # Step 1: record pending and return.
                    session.add(
                        Approval(
                            proposal_id=row.id,
                            user_id=body.user_id,
                            approved_at=_utcnow(),
                            approval_channel=body.channel,
                            second_factor_used=False,
                            signed_token_id="pending",
                        )
                    )
                    session.add(
                        ProposalHistory(
                            proposal_id=row.id,
                            status=row.status,
                            transitioned_at=_utcnow(),
                            transitioned_by=f"user:{body.user_id}",
                            note="T3 delay-mode: first approve recorded (pending)",
                        )
                    )
                    await session.commit()
                    return ProposalActionResponse(
                        status="pending_delay",
                        proposal_id=row.id,
                        message=(
                            f"First approve recorded; wait "
                            f"{settings.security.delay_minutes} minutes "
                            "and approve again to commit."
                        ),
                    )
                # Step 2: ensure delay elapsed.
                elapsed = _utcnow() - first_pending.approved_at
                required = _td(minutes=settings.security.delay_minutes)
                if elapsed < required:
                    remaining = (required - elapsed).total_seconds() / 60.0
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Delay second-factor: wait "
                            f"{remaining:.1f} more minutes before commit."
                        ),
                    )
                # Mark first_pending as confirmed.
                first_pending.signed_token_id = "delay_confirmed"
                body.second_factor = True

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
