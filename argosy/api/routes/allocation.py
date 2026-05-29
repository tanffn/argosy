"""Generic allocation-action routes — Accept/Defer/list for any source.

Sprint commit #6b of the plan/execute/monitor reorg. Generalizes the
existing /retirement/windfall/{accept,defer,actions} routes (which stay
in place for backward compat) over the `action_source` discriminator
added in migration 0041:

  - action_source='windfall'         (existing windfall flow)
  - action_source='unallocated_cash' (UnallocatedCashCard on /portfolio)
  - action_source='monitor_drift'    (monitor agent — sprint commit #11)
  - action_source='life_event'       (UpcomingVestCard CTA — spec #2)
  - action_source='rebalance'        (plan-driven rebalance — later)
  - action_source='manual'           (user free-text entry — later)

Mounts at /api/proposals/allocation/* — i.e. the allocation queue lives
under /proposals (per the IA reorg in spec #1 §2).
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.state.models import AllocationAction


router = APIRouter(prefix="/proposals/allocation", tags=["allocation"])


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


# Mirrors the action_source CHECK in migration 0041. Pydantic Literal
# gives a 422 at request-validation time when the client sends a value
# we don't recognise, before the DB CHECK fires.
ActionSourceLiteral = Literal[
    "windfall",
    "unallocated_cash",
    "monitor_drift",
    "rebalance",
    "life_event",
    "manual",
]


class AllocationActionRequest(BaseModel):
    """Payload for POST /accept and POST /defer.

    `source_ref` is a free-form string scoped to `action_source`; for
    `unallocated_cash` we use JSON of {snapshot_date, horizon,
    asset_class, instrument} so the (user_id, action_source, source_ref)
    unique index catches double-Accept on the same proposal but allows
    distinct proposals from the same snapshot to coexist.
    """

    user_id: str
    action_source: ActionSourceLiteral
    source_detected_at: datetime
    source_ref: str | None = None
    # Proposal shape (frozen at decision time per migration 0041 docstring)
    horizon: str
    asset_class: str
    instrument: str
    amount_usd: float
    rationale: str
    closes_delta_usd: float
    confidence: str
    # Defer-only
    due_date: date | None = None
    user_note: str | None = None


class AllocationActionResponse(BaseModel):
    id: int
    decided_status: str
    decided_at: datetime
    due_date: date | None


class AllocationActionListItem(BaseModel):
    id: int
    action_source: str
    source_detected_at: datetime
    source_ref: str | None
    horizon: str
    asset_class: str
    instrument: str
    amount_usd: float
    decided_status: str
    decided_at: datetime
    due_date: date | None
    user_note: str | None
    proposal_id: int | None


class AllocationActionsListResponse(BaseModel):
    actions: list[AllocationActionListItem]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persist(
    payload: AllocationActionRequest,
    decided_status: str,
    db: Session,
) -> AllocationAction:
    row = AllocationAction(
        user_id=payload.user_id,
        action_source=payload.action_source,
        source_detected_at=payload.source_detected_at,
        source_ref=payload.source_ref,
        horizon=payload.horizon,
        asset_class=payload.asset_class,
        instrument=payload.instrument,
        amount_usd=payload.amount_usd,
        rationale=payload.rationale,
        closes_delta_usd=payload.closes_delta_usd,
        confidence=payload.confidence,
        decided_status=decided_status,
        due_date=payload.due_date,
        user_note=payload.user_note,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _to_list_item(row: AllocationAction) -> AllocationActionListItem:
    return AllocationActionListItem(
        id=row.id,
        action_source=row.action_source,
        source_detected_at=row.source_detected_at,
        source_ref=row.source_ref,
        horizon=row.horizon,
        asset_class=row.asset_class,
        instrument=row.instrument,
        amount_usd=float(row.amount_usd),
        decided_status=row.decided_status,
        decided_at=row.decided_at,
        due_date=row.due_date,
        user_note=row.user_note,
        proposal_id=row.proposal_id,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/accept", response_model=AllocationActionResponse)
def post_accept(
    payload: AllocationActionRequest,
    db: Session = Depends(get_db),
) -> AllocationActionResponse:
    """Record an Accept on an allocation proposal from any source.

    Per Ariel's Q4 answer in the design phase ("Log the data, I will
    manually buy"), this is intent-persistence only. No auto-promotion
    to /proposals trade execution; the user executes manually in their
    broker. The accepted row sits with `decided_status='accepted'` until
    either (a) the user manually marks it 'executed' (no UI for that
    yet), (b) the action_engine promotion lands (separate future
    commit), or (c) it expires by stale-by-time policy (also future).
    """
    row = _persist(payload, "accepted", db)
    return AllocationActionResponse(
        id=row.id,
        decided_status=row.decided_status,
        decided_at=row.decided_at,
        due_date=row.due_date,
    )


@router.post("/defer", response_model=AllocationActionResponse)
def post_defer(
    payload: AllocationActionRequest,
    db: Session = Depends(get_db),
) -> AllocationActionResponse:
    """Record a Defer on an allocation proposal from any source.

    Same shape as /accept; optional `due_date` stamps when the user wants
    to be re-prompted. Without it the row sits as an open deferral
    until the user changes their mind or it expires.
    """
    row = _persist(payload, "deferred", db)
    return AllocationActionResponse(
        id=row.id,
        decided_status=row.decided_status,
        decided_at=row.decided_at,
        due_date=row.due_date,
    )


@router.get("/actions", response_model=AllocationActionsListResponse)
def list_actions(
    user_id: str,
    action_source: ActionSourceLiteral | None = None,
    source_ref: str | None = None,
    db: Session = Depends(get_db),
) -> AllocationActionsListResponse:
    """List recorded allocation actions for a user.

    Optional filters by `action_source` (windfall / unallocated_cash /
    etc.) and `source_ref` (specific snapshot/event/etc.) let the UI
    render per-proposal feedback inline ("✓ Accepted at 14:32" /
    "↻ Deferred · due 2026-06-15") without scanning the whole history.

    The most common UI pattern: a card knows its current source_ref and
    queries `?user_id=X&action_source=unallocated_cash&source_ref=<JSON>`
    to find prior decisions on the proposals it's about to render.
    """
    q = db.query(AllocationAction).filter(AllocationAction.user_id == user_id)
    if action_source is not None:
        q = q.filter(AllocationAction.action_source == action_source)
    if source_ref is not None:
        q = q.filter(AllocationAction.source_ref == source_ref)
    rows = q.order_by(AllocationAction.decided_at.desc()).all()
    return AllocationActionsListResponse(
        actions=[_to_list_item(r) for r in rows]
    )


__all__ = ["router"]
