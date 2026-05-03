"""Execution API (Phase 4).

Endpoints:
  POST /api/proposals/{id}/execute       — admin/dev: drive ExecutionRouter
  GET  /api/proposals/{id}/approve       — email-link landing endpoint
  GET  /api/lots                          — list lots (filterable)
  GET  /api/fills                         — list fills (filterable)
  GET  /api/audit                         — list audit_log rows (filterable)

The execute route is intentionally *not* exposed in the main UI; the
proposals page wires it explicitly when a user clicks "Execute now". The
approve-via-token route only verifies the token and redirects to the
dashboard with a confirm dialog (SDD §10.2 anti-phishing).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from argosy.api.events import publish_event
from argosy.billing.entitlements import Entitlements, feature_required_tier
from argosy.channels.email import EmailApprovalLink, EmailSettings
from argosy.decisions.proposals import IllegalTransitionError
from argosy.execution.router import ExecutionRouter
from argosy.logging import get_logger
from argosy.state import db as db_mod
from argosy.state.models import (
    AuditLog,
    Fill as FillRow,
    Lot as LotRow,
)


_log = get_logger("argosy.api.execution")
router = APIRouter(tags=["execution"])


# ----------------------------------------------------------------------
# DTOs
# ----------------------------------------------------------------------


class ExecuteRequest(BaseModel):
    user_id: str = "ariel"
    cash_available_usd: float = 0.0
    max_position_usd: float | None = None
    plan_targets: dict[str, float] | None = None
    snapshot_pct: dict[str, float] | None = None
    day_pnl_usd: float = 0.0
    daily_loss_limit_usd: float | None = None


class ExecuteResponse(BaseModel):
    status: str
    proposal_id: int
    broker: str
    broker_order_id: str
    paper: bool
    reason: str
    fills: list[dict[str, Any]]


class LotItem(BaseModel):
    id: int
    user_id: str
    account_id: str
    ticker: str
    lot_id_external: str
    quantity: float
    cost_basis_usd: float
    acquired_at: str | None
    source: str
    imported_at: str


class LotsResponse(BaseModel):
    rows: list[LotItem]
    total: int


class FillItem(BaseModel):
    id: int
    user_id: str
    proposal_id: int | None
    broker: str
    broker_order_id: str
    ticker: str
    action: str
    quantity: float
    price: float
    commission: float
    filled_at: str
    paper: bool


class FillsResponse(BaseModel):
    rows: list[FillItem]
    total: int


class AuditItem(BaseModel):
    id: int
    user_id: str
    event_type: str
    entity_type: str
    entity_id: str
    payload_json: str
    created_at: str


class AuditResponse(BaseModel):
    rows: list[AuditItem]
    total: int


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


@router.post("/proposals/{proposal_id}/execute", response_model=ExecuteResponse)
async def execute_proposal(
    proposal_id: int,
    body: ExecuteRequest,
) -> ExecuteResponse:
    # Live execution requires the live_execution entitlement.
    # Paper-mode is always allowed (the router resolves mode internally
    # via agent_settings; we pre-block live for tenants without the
    # entitlement so we don't even attempt).
    settings_obj = None
    try:
        from argosy.agent_settings import load_agent_settings as _load

        settings_obj = _load(body.user_id)
    except Exception:  # pragma: no cover - defensive
        pass
    if settings_obj is not None and settings_obj.execution.default_mode == "live":
        ent = Entitlements.load(body.user_id)
        if not ent.has("live_execution"):
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "feature_not_entitled",
                    "feature": "live_execution",
                    "required_tier": feature_required_tier(
                        "live_execution"
                    ).value,
                    "plan": ent.plan.value,
                },
            )

    routerz = ExecutionRouter(user_id=body.user_id)
    try:
        result = await routerz.execute(
            proposal_id,
            cash_available_usd=body.cash_available_usd,
            max_position_usd=body.max_position_usd,
            plan_targets=body.plan_targets or {},
            snapshot_pct=body.snapshot_pct or {},
            day_pnl_usd=body.day_pnl_usd,
            daily_loss_limit_usd=body.daily_loss_limit_usd,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ExecuteResponse(
        status=result.status,
        proposal_id=proposal_id,
        broker=result.broker,
        broker_order_id=result.broker_order_id,
        paper=result.paper,
        reason=result.reason,
        fills=[f.model_dump(mode="json") for f in result.fills],
    )


@router.get("/proposals/{proposal_id}/approve")
async def approve_via_token(
    proposal_id: int,
    token: str = Query(..., description="Signed approval token from email"),
    user_id: str = Query("ariel"),
) -> RedirectResponse:
    """Email-link landing. Verifies the token; redirects to the dashboard.

    Per SDD §10.2, never approve directly here — phishing surface. The
    redirect carries the token so the dashboard can show a confirm dialog
    that POSTs to `/api/proposals/{id}/approve` with `channel=email` and
    the token id.
    """
    link = EmailApprovalLink()
    payload = link.verify(token)
    if payload is None:
        raise HTTPException(status_code=400, detail="invalid or expired token")
    if payload.proposal_id != proposal_id:
        raise HTTPException(status_code=400, detail="token/proposal mismatch")
    if payload.user_id != user_id and user_id != "ariel":
        # Phase 4 single-user; lenient check.
        raise HTTPException(status_code=403, detail="token/user mismatch")

    settings = EmailSettings.load(user_id)
    base = settings.public_url.rstrip("/")
    # Dashboard at :1337 in dev. The approve-via-token landing always
    # bounces the user to the dashboard's proposal screen.
    if base.endswith(":8000"):
        dash_url = base.replace(":8000", ":1337")
    else:
        dash_url = base
    target = (
        f"{dash_url}/proposals?confirm={proposal_id}"
        f"&action={payload.action}&token={token}"
    )
    return RedirectResponse(url=target, status_code=302)


@router.get("/lots", response_model=LotsResponse)
async def list_lots(
    user_id: str = Query("ariel"),
    account_id: str | None = Query(None),
    ticker: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> LotsResponse:
    async with db_mod.get_session() as session:
        from sqlalchemy import func

        count_stmt = select(func.count(LotRow.id)).where(LotRow.user_id == user_id)
        stmt = select(LotRow).where(LotRow.user_id == user_id)
        if account_id:
            stmt = stmt.where(LotRow.account_id == account_id)
            count_stmt = count_stmt.where(LotRow.account_id == account_id)
        if ticker:
            stmt = stmt.where(LotRow.ticker == ticker.upper())
            count_stmt = count_stmt.where(LotRow.ticker == ticker.upper())

        total = (await session.execute(count_stmt)).scalar_one()
        rows = (
            await session.execute(stmt.order_by(LotRow.imported_at.desc()).limit(limit).offset(offset))
        ).scalars().all()

        return LotsResponse(
            rows=[
                LotItem(
                    id=r.id,
                    user_id=r.user_id,
                    account_id=r.account_id,
                    ticker=r.ticker,
                    lot_id_external=r.lot_id_external,
                    quantity=float(r.quantity),
                    cost_basis_usd=float(r.cost_basis_usd),
                    acquired_at=r.acquired_at.isoformat() if r.acquired_at else None,
                    source=r.source,
                    imported_at=r.imported_at.isoformat(),
                )
                for r in rows
            ],
            total=int(total),
        )


@router.get("/fills", response_model=FillsResponse)
async def list_fills(
    user_id: str = Query("ariel"),
    proposal_id: int | None = Query(None),
    ticker: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> FillsResponse:
    async with db_mod.get_session() as session:
        from sqlalchemy import func

        count_stmt = select(func.count(FillRow.id)).where(FillRow.user_id == user_id)
        stmt = select(FillRow).where(FillRow.user_id == user_id)
        if proposal_id is not None:
            stmt = stmt.where(FillRow.proposal_id == proposal_id)
            count_stmt = count_stmt.where(FillRow.proposal_id == proposal_id)
        if ticker:
            stmt = stmt.where(FillRow.ticker == ticker.upper())
            count_stmt = count_stmt.where(FillRow.ticker == ticker.upper())

        total = (await session.execute(count_stmt)).scalar_one()
        rows = (
            await session.execute(stmt.order_by(FillRow.filled_at.desc()).limit(limit).offset(offset))
        ).scalars().all()

        return FillsResponse(
            rows=[
                FillItem(
                    id=r.id,
                    user_id=r.user_id,
                    proposal_id=r.proposal_id,
                    broker=r.broker,
                    broker_order_id=r.broker_order_id,
                    ticker=r.ticker,
                    action=r.action,
                    quantity=float(r.quantity),
                    price=float(r.price),
                    commission=float(r.commission),
                    filled_at=r.filled_at.isoformat(),
                    paper=bool(r.paper),
                )
                for r in rows
            ],
            total=int(total),
        )


@router.get("/audit", response_model=AuditResponse)
async def list_audit_log(
    user_id: str = Query("ariel"),
    event_type: str | None = Query(None),
    entity_type: str | None = Query(None),
    entity_id: str | None = Query(None),
    since: str | None = Query(None, description="ISO datetime; rows with created_at >= since"),
    until: str | None = Query(None, description="ISO datetime; rows with created_at < until"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> AuditResponse:
    async with db_mod.get_session() as session:
        from sqlalchemy import func

        count_stmt = select(func.count(AuditLog.id)).where(AuditLog.user_id == user_id)
        stmt = select(AuditLog).where(AuditLog.user_id == user_id)
        if event_type:
            stmt = stmt.where(AuditLog.event_type == event_type)
            count_stmt = count_stmt.where(AuditLog.event_type == event_type)
        if entity_type:
            stmt = stmt.where(AuditLog.entity_type == entity_type)
            count_stmt = count_stmt.where(AuditLog.entity_type == entity_type)
        if entity_id:
            stmt = stmt.where(AuditLog.entity_id == entity_id)
            count_stmt = count_stmt.where(AuditLog.entity_id == entity_id)

        for raw, op in ((since, ">="), (until, "<")):
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"bad datetime {raw!r}"
                ) from exc
            cond = AuditLog.created_at >= dt if op == ">=" else AuditLog.created_at < dt
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)

        total = (await session.execute(count_stmt)).scalar_one()
        rows = (
            await session.execute(stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset))
        ).scalars().all()

        return AuditResponse(
            rows=[
                AuditItem(
                    id=r.id,
                    user_id=r.user_id,
                    event_type=r.event_type,
                    entity_type=r.entity_type,
                    entity_id=r.entity_id,
                    payload_json=r.payload_json,
                    created_at=r.created_at.isoformat(),
                )
                for r in rows
            ],
            total=int(total),
        )


# Suppress unused-import warning for publish_event; reserved for Phase 5.
_ = publish_event


__all__ = ["router"]
