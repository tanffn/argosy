"""Action-proposals REST surface — Spec E commit #6.

Mounted at ``/api/proposals/actions/*`` — sibling to the existing
trade-order ``/api/proposals/*`` routes (which live in
``argosy/api/routes/proposals.py``). The path namespace keeps the
two router files cleanly separated without colliding on path-int
captures (``/api/proposals/{id}`` would conflict with
``/api/proposals/actions`` if both routers shared the same prefix).

Four routes, one per spec §6 verb:

  * ``GET  /api/proposals/actions?user_id=&status=open``
        — list ActionProposal rows for the /proposals page.
  * ``POST /api/proposals/actions/{id}/accept``
        — body: optional ``custom_payload`` for the Customize flow.
  * ``POST /api/proposals/actions/{id}/defer``
        — body: optional ``defer_until`` (ISO date) + ``note``.
  * ``POST /api/proposals/actions/{id}/reject``
        — body: optional ``reason``.

Auth: Phase 3 — multi-tenant by ``user_id`` query / body parameter
(default ``"ariel"`` per the single-user contract); Phase 5 layers
real auth + per-proposal authorization. The lookup helpers already
defend against cross-tenant access by returning 404 on
``ProposalNotFoundError`` (which the service raises both for
truly-missing rows AND for rows belonging to another tenant).

The route layer maps service exceptions to HTTP:

  * ``ProposalNotFoundError``    -> 404
  * ``InvalidProposalStateError`` -> 409 (Conflict)
  * Any other unexpected exception bubbles to FastAPI's default
    500 handler with a structured log on the way out.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.action_proposals import (
    InvalidProposalStateError,
    ProposalNotFoundError,
    accept_action_proposal,
    defer_action_proposal,
    list_open_action_proposals,
    reject_action_proposal,
    to_view,
)


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/proposals/actions", tags=["action-proposals"])


# ---------------------------------------------------------------------------
# DTOs (FastAPI Pydantic models — mirror the service's ActionProposalView)
# ---------------------------------------------------------------------------


class ActionProposalDTO(BaseModel):
    """JSON shape returned by GET /api/proposals/actions[/{id}/...].

    Mirrors ``argosy.services.action_proposals.ActionProposalView`` —
    isolation between service and route layers means the service
    module stays Pydantic-free + the route module owns the wire
    contract.
    """

    id: int
    user_id: str
    kind: str
    severity: Literal["info", "warning", "critical"]
    status: str
    execution_state: str
    summary: str
    rationale_md: str
    suggested_payload: dict[str, Any]
    surfaced_at: str
    expires_at: str
    decided_at: str | None
    decided_by_user_note: str | None
    source_flag_id: int | None
    source_observation_id: int | None
    source_inferred_event_id: int | None


class ActionProposalListResponse(BaseModel):
    rows: list[ActionProposalDTO]
    total: int


class AcceptRequest(BaseModel):
    """Body for POST /api/proposals/actions/{id}/accept.

    ``custom_payload`` is optional. When present (the Customize flow)
    the user-edited payload replaces the LLM's suggested_payload in
    place; the original is logged via ``decided_by_user_note``. When
    absent (plain Accept) the proposal is accepted with the
    suggested_payload unchanged.
    """

    user_id: str = "ariel"
    custom_payload: dict[str, Any] | None = None


class DeferRequest(BaseModel):
    """Body for POST /api/proposals/actions/{id}/defer.

    ``defer_until_date`` is an ISO date string ("YYYY-MM-DD"). When
    present, the v1 service encodes it into ``decided_by_user_note``
    so the housekeeping loop can parse it and re-open the row on the
    requested date (loop ships in a follow-on commit).
    """

    user_id: str = "ariel"
    defer_until_date: str | None = None
    note: str | None = None


class RejectRequest(BaseModel):
    user_id: str = "ariel"
    reason: str | None = None


class ActionProposalActionResponse(BaseModel):
    status: Literal["ok"]
    proposal: ActionProposalDTO


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _view_to_dto(view: Any) -> ActionProposalDTO:
    """Adapt the service's ActionProposalView dataclass to the
    Pydantic DTO. ``view`` is intentionally typed as ``Any`` to keep
    the import cleanly one-way (service does not import from route).
    """
    return ActionProposalDTO(
        id=view.id,
        user_id=view.user_id,
        kind=view.kind,
        severity=view.severity,  # type: ignore[arg-type]
        status=view.status,
        execution_state=view.execution_state,
        summary=view.summary,
        rationale_md=view.rationale_md,
        suggested_payload=view.suggested_payload,
        surfaced_at=view.surfaced_at,
        expires_at=view.expires_at,
        decided_at=view.decided_at,
        decided_by_user_note=view.decided_by_user_note,
        source_flag_id=view.source_flag_id,
        source_observation_id=view.source_observation_id,
        source_inferred_event_id=view.source_inferred_event_id,
    )


def _resolve_not_found(exc: ProposalNotFoundError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def _resolve_conflict(exc: InvalidProposalStateError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


def _parse_defer_date(raw: str | None) -> date | None:
    if raw is None or raw == "":
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"defer_until_date must be ISO YYYY-MM-DD, got {raw!r}: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=ActionProposalListResponse)
def list_actions(
    user_id: str = Query("ariel"),
    status: str = Query("open"),
    db: Session = Depends(get_db),
) -> ActionProposalListResponse:
    """List action_proposals for the user.

    v1 only supports ``status=open`` (the only status the UI
    currently renders); the parameter is accepted as a placeholder
    for a future "Recent decisions" view (spec §6.1 section 2).
    Unrecognized values fall through to the same open-only response;
    no 422 here — the UI just wouldn't have anything else to show.
    """
    if status != "open":
        # Forward-compat: 'recent' / 'all' filters land in a future
        # commit; v1 returns an empty list for unknown filters rather
        # than 422-ing so a stale UI doesn't break.
        return ActionProposalListResponse(rows=[], total=0)

    rows = list_open_action_proposals(db, user_id)
    views = [to_view(r) for r in rows]
    return ActionProposalListResponse(
        rows=[_view_to_dto(v) for v in views],
        total=len(views),
    )


@router.post(
    "/{proposal_id}/accept", response_model=ActionProposalActionResponse,
)
def accept_action(
    proposal_id: int,
    body: AcceptRequest,
    db: Session = Depends(get_db),
) -> ActionProposalActionResponse:
    try:
        row = accept_action_proposal(
            db,
            proposal_id,
            user_id=body.user_id,
            custom_payload=body.custom_payload,
        )
    except ProposalNotFoundError as exc:
        raise _resolve_not_found(exc) from exc
    except InvalidProposalStateError as exc:
        raise _resolve_conflict(exc) from exc
    view = to_view(row)
    return ActionProposalActionResponse(status="ok", proposal=_view_to_dto(view))


@router.post(
    "/{proposal_id}/defer", response_model=ActionProposalActionResponse,
)
def defer_action(
    proposal_id: int,
    body: DeferRequest,
    db: Session = Depends(get_db),
) -> ActionProposalActionResponse:
    defer_dt = _parse_defer_date(body.defer_until_date)
    try:
        row = defer_action_proposal(
            db,
            proposal_id,
            defer_dt,
            user_id=body.user_id,
            note=body.note,
        )
    except ProposalNotFoundError as exc:
        raise _resolve_not_found(exc) from exc
    except InvalidProposalStateError as exc:
        raise _resolve_conflict(exc) from exc
    view = to_view(row)
    return ActionProposalActionResponse(status="ok", proposal=_view_to_dto(view))


@router.post(
    "/{proposal_id}/reject", response_model=ActionProposalActionResponse,
)
def reject_action(
    proposal_id: int,
    body: RejectRequest,
    db: Session = Depends(get_db),
) -> ActionProposalActionResponse:
    try:
        row = reject_action_proposal(
            db,
            proposal_id,
            user_id=body.user_id,
            reason=body.reason,
        )
    except ProposalNotFoundError as exc:
        raise _resolve_not_found(exc) from exc
    except InvalidProposalStateError as exc:
        raise _resolve_conflict(exc) from exc
    view = to_view(row)
    return ActionProposalActionResponse(status="ok", proposal=_view_to_dto(view))


__all__ = ["router"]
