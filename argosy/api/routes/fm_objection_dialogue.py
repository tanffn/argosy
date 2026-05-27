"""API surface for the FM-objection ZigZag.

Two endpoints, both under ``/api/plan/draft/objections/{idx}/...``:

  * ``POST .../discuss``    — kick off a 3-turn FM↔analyst dialogue for
    one objection. Returns ``decision_run_id`` immediately; the actual
    LLM work runs on a background thread. UI subscribes to WS events
    ``plan.fm_objection.dialogue.{started,completed}`` for completion.
  * ``GET  .../dialogues``  — re-render prior dialogues for one
    objection so the UI doesn't lose dialogue state on page reload.

The router is sibling to ``plan.py`` so the dialogue feature can ship
without rebasing on the (concurrently edited) plan router. Same
``/plan/draft/objections/*`` URL prefix.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.api.routes.plan import (
    FMObjection,
    _classify_severity,
    _parse_fm_response,
    _split_reason,
    get_db,
)
from argosy.state.models import AgentReport, PlanVersion
from argosy.state.queries import get_pending_draft

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plan", tags=["plan"])


# ---------------------------------------------------------------------
# Pydantic DTOs
# ---------------------------------------------------------------------


class DiscussObjectionRequest(BaseModel):
    user_id: str
    analyst_role: str  # one of ANALYST_AGENT_NAME_TO_ROLE values


class DiscussObjectionResponse(BaseModel):
    status: str  # "dialogue_started" | "dialogue_inflight" | "cost_cap_refused"
    decision_run_id: int | None
    inflight: bool
    detail: str | None = None


class DialogueRowDTO(BaseModel):
    decision_run_id: int
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    objection_index: int
    analyst_role: str
    resolution: (
        Literal[
            "FM_ACCEPTS_ANALYST",
            "FM_MAINTAINS_OBJECTION",
            "FM_REVISES_OBJECTION",
            "ESCALATE_TO_USER",
        ]
        | None
    ) = None
    analyst_stance: Literal["CONCEDE", "REBUT", "CLARIFY"] | None = None
    analyst_reasoning_md: str | None = None
    analyst_suggested_fix: str | None = None
    fm_reasoning_md: str | None = None
    updated_objection_text: str | None = None
    suggested_plan_amendment: str | None = None
    cited_sources: list[str] = []


class DialoguesListResponse(BaseModel):
    objection_index: int
    plan_version_id: int
    dialogues: list[DialogueRowDTO]


# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------


def _resolve_objection_by_index(
    db: Session, *, user_id: str, objection_index: int,
) -> tuple[PlanVersion, FMObjection, str]:
    """Locate the FM objection at ``objection_index`` on the user's draft.

    Returns ``(plan_version, FMObjection, decision_audit_token)``.

    Raises HTTPException on:
      * no pending draft (404)
      * draft has no FM verdict (400)
      * objection_index out of range for the parsed list (404)
    """
    pv = get_pending_draft(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no pending draft for user")
    if pv.decision_run_id is None:
        raise HTTPException(
            status_code=400,
            detail="draft has no synthesis run; no FM objections to discuss",
        )
    decision_id_str = f"plan-synth-{pv.decision_run_id}"
    fm_row = db.execute(
        select(AgentReport).where(
            AgentReport.user_id == user_id,
            AgentReport.decision_id == decision_id_str,
            AgentReport.agent_role == "fund_manager",
        ).order_by(desc(AgentReport.created_at)).limit(1)
    ).scalar_one_or_none()
    if fm_row is None or not fm_row.response_text:
        raise HTTPException(
            status_code=400,
            detail="no fund_manager agent_report for this draft",
        )
    parsed = _parse_fm_response(fm_row.response_text)
    reasons = parsed.get("reasons") or []
    objections: list[FMObjection] = []
    for r in reasons:
        if not isinstance(r, str) or not r.strip():
            continue
        topic, detail = _split_reason(r)
        objections.append(
            FMObjection(
                severity=_classify_severity(topic, detail),
                topic=topic,
                detail=detail,
            )
        )
    # Sort to match GET /draft/objections ordering (RED → AMBER → YELLOW),
    # which is what the UI keys its objection_index against.
    sev_order = {"RED": 0, "AMBER": 1, "YELLOW": 2}
    objections.sort(key=lambda o: sev_order.get(o.severity, 99))

    if objection_index < 0 or objection_index >= len(objections):
        raise HTTPException(
            status_code=404,
            detail=(
                f"objection_index={objection_index} out of range "
                f"(have {len(objections)} objections)"
            ),
        )
    return pv, objections[objection_index], decision_id_str


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------


@router.post(
    "/draft/objections/{idx}/discuss",
    response_model=DiscussObjectionResponse,
)
def post_discuss_objection(
    idx: int,
    body: DiscussObjectionRequest,
    db: Session = Depends(get_db),
) -> DiscussObjectionResponse:
    """Kick off the 3-turn FM↔analyst dialogue for one objection.

    Background-threaded; returns immediately with the new
    ``decision_run_id`` (or the existing one when the idempotency
    window short-circuits a double-click). When the per-user 24h spend
    has consumed enough headroom that the estimated $0.50/dialogue
    cost would breach ``ARGOSY_SYNTHESIS_COST_CAP_USD``, returns a
    200 with ``status="cost_cap_refused"`` and ``decision_run_id=null``
    so the UI can surface a clean message rather than a 500.
    """
    from argosy.agents.analyst_responder import (
        ANALYST_AGENT_NAME_TO_ROLE,
        ANALYST_ROLE_TO_AGENT_NAME,
    )
    from argosy.orchestrator.flows.fm_objection_dialogue import (
        CostCapExceededError,
        InvalidAnalystRoleError,
        start_fm_objection_dialogue,
    )

    if not body.user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    analyst_role = (body.analyst_role or "").strip().lower()
    if analyst_role not in set(ANALYST_AGENT_NAME_TO_ROLE.values()):
        raise HTTPException(
            status_code=400,
            detail=(
                f"analyst_role {body.analyst_role!r} not in canonical map. "
                f"Allowed: {sorted(set(ANALYST_AGENT_NAME_TO_ROLE.values()))}"
            ),
        )

    pv, objection, decision_audit_token = _resolve_objection_by_index(
        db, user_id=body.user_id, objection_index=idx,
    )

    # Optional cross-check: when the objection text mentions specific
    # agent_report:... refs, verify the requested analyst_role is among
    # them. Soft-check (warning only) so the user can still discuss a
    # role we couldn't auto-detect from the text.
    from argosy.orchestrator.flows.fm_objection_dialogue import (
        parse_agent_refs_from_objection,
    )
    refs = parse_agent_refs_from_objection(objection.detail)
    if refs:
        allowed_roles = {ANALYST_AGENT_NAME_TO_ROLE[name] for name in refs}
        if analyst_role not in allowed_roles:
            logger.warning(
                "fm_objection_dialogue.role_not_referenced "
                "user_id=%s idx=%s requested_role=%s refs=%s",
                body.user_id, idx, analyst_role, refs,
            )

    try:
        result = start_fm_objection_dialogue(
            db,
            user_id=body.user_id,
            plan_version_id=pv.id,
            objection_index=idx,
            analyst_role=analyst_role,
            objection_topic=objection.topic,
            objection_detail=objection.detail,
            objection_severity=objection.severity,
            prior_decision_audit_token=decision_audit_token,
        )
    except InvalidAnalystRoleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CostCapExceededError as exc:
        return DiscussObjectionResponse(
            status="cost_cap_refused",
            decision_run_id=None,
            inflight=False,
            detail=str(exc),
        )

    return DiscussObjectionResponse(
        status="dialogue_inflight" if result.inflight else "dialogue_started",
        decision_run_id=result.decision_run_id,
        inflight=result.inflight,
        detail=ANALYST_ROLE_TO_AGENT_NAME.get(analyst_role, analyst_role),
    )


@router.get(
    "/draft/objections/{idx}/dialogues",
    response_model=DialoguesListResponse,
)
def get_objection_dialogues(
    idx: int,
    user_id: str,
    db: Session = Depends(get_db),
) -> DialoguesListResponse:
    """Return prior dialogues for one objection on the current draft.

    Filters the user's ``decision_runs`` (decision_kind=
    ``"fm_objection_dialogue"``) down to those whose notes_json carries
    ``plan_version_id == current_draft.id`` AND
    ``objection_index == idx``. Newest first. Each row carries the
    structured outcome so the UI can re-render without an LLM call.
    """
    from argosy.orchestrator.flows.fm_objection_dialogue import (
        list_dialogues_for_plan_version,
    )

    pv = get_pending_draft(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no pending draft for user")

    rows = list_dialogues_for_plan_version(
        db, user_id=user_id, plan_version_id=pv.id,
    )
    out: list[DialogueRowDTO] = []
    for row in rows:
        notes = row.get("notes") or {}
        if notes.get("objection_index") != idx:
            continue
        out.append(
            DialogueRowDTO(
                decision_run_id=row["decision_run_id"],
                status=row["status"],
                started_at=row.get("started_at"),
                finished_at=row.get("finished_at"),
                objection_index=int(notes.get("objection_index") or 0),
                analyst_role=str(notes.get("analyst_role") or ""),
                resolution=notes.get("resolution"),
                analyst_stance=notes.get("analyst_stance"),
                analyst_reasoning_md=notes.get("analyst_reasoning_md"),
                analyst_suggested_fix=notes.get("analyst_suggested_fix"),
                fm_reasoning_md=notes.get("fm_reasoning_md"),
                updated_objection_text=notes.get("updated_objection_text"),
                suggested_plan_amendment=notes.get("suggested_plan_amendment"),
                cited_sources=list(notes.get("cited_sources") or []),
            )
        )
    return DialoguesListResponse(
        objection_index=idx,
        plan_version_id=pv.id,
        dialogues=out,
    )


__all__ = [
    "DialogueRowDTO",
    "DialoguesListResponse",
    "DiscussObjectionRequest",
    "DiscussObjectionResponse",
    "router",
]
