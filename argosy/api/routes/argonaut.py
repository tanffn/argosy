"""Argonaut API routes (SDD §11.1 screen #5, Phase 5).

Endpoints:
  GET  /api/argonaut/status?user_id=
  GET  /api/argonaut/snapshots?user_id=&limit=
  POST /api/argonaut/mode    {user_id, mode}    paper | live | queue_only
  POST /api/argonaut/snapshot {user_id}         force a daily snapshot
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from argosy.accounts.argonaut import ArgonautAccount
from argosy.accounts.persistence import list_snapshots
from argosy.agent_settings import (
    AgentSettings,
    load_agent_settings,
    save_agent_settings,
)
from argosy.api.events import publish_event
from argosy.execution.audit import record_audit_event
from argosy.state import db as db_mod
from argosy.state.models import Fill as FillRow


router = APIRouter(prefix="/argonaut", tags=["argonaut"])


# ----------------------------------------------------------------------
# DTOs
# ----------------------------------------------------------------------


class ArgonautStatusDTO(BaseModel):
    user_id: str
    account_id: str
    size_usd: float
    execution_mode: str
    autonomy_enabled: bool
    per_decision_max_pct: float
    daily_loss_limit_pct: float
    open_positions: list[dict[str, Any]]


class ArgonautSnapshotDTO(BaseModel):
    date: str
    total_value_usd: float
    cash_usd: float
    positions_value_usd: float
    day_pnl_usd: float


class ArgonautSnapshotsResponse(BaseModel):
    rows: list[ArgonautSnapshotDTO]


class ModeRequest(BaseModel):
    user_id: str = "ariel"
    mode: Literal["paper", "live", "queue_only"]


class ModeResponse(BaseModel):
    status: str
    mode: str
    message: str = ""


class TradesResponse(BaseModel):
    rows: list[dict[str, Any]]


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


@router.get("/status", response_model=ArgonautStatusDTO)
async def argonaut_status(
    user_id: str = Query("ariel"),
) -> ArgonautStatusDTO:
    settings = load_agent_settings(user_id)
    acct = ArgonautAccount(user_id=user_id, settings=settings)
    positions = acct.get_open_positions()
    return ArgonautStatusDTO(
        user_id=user_id,
        account_id=acct.account_id,
        size_usd=acct.configured_size_usd,
        execution_mode=acct.current_execution_mode(),
        autonomy_enabled=acct.is_autonomy_enabled(),
        per_decision_max_pct=acct.per_decision_max_pct,
        daily_loss_limit_pct=acct.daily_loss_limit_pct,
        open_positions=[
            {
                "ticker": p.ticker,
                "quantity": p.quantity,
                "avg_cost": p.avg_cost,
                "currency": p.currency,
                "asset_class": p.asset_class,
            }
            for p in positions
        ],
    )


@router.get("/snapshots", response_model=ArgonautSnapshotsResponse)
async def argonaut_snapshots(
    user_id: str = Query("ariel"),
    limit: int = Query(365, ge=1, le=3650),
) -> ArgonautSnapshotsResponse:
    rows = await list_snapshots(user_id=user_id, limit=limit)
    return ArgonautSnapshotsResponse(
        rows=[
            ArgonautSnapshotDTO(
                date=r.date,
                total_value_usd=float(r.total_value_usd),
                cash_usd=float(r.cash_usd),
                positions_value_usd=float(r.positions_value_usd),
                day_pnl_usd=float(r.day_pnl_usd),
            )
            for r in rows
        ]
    )


@router.post("/mode", response_model=ModeResponse)
async def set_mode(body: ModeRequest) -> ModeResponse:
    settings = load_agent_settings(body.user_id)
    prior = settings.limited_account.execution_mode
    if prior == body.mode:
        return ModeResponse(
            status="noop", mode=body.mode, message=f"already {body.mode}"
        )
    settings.limited_account.execution_mode = body.mode
    save_agent_settings(body.user_id, settings)
    await record_audit_event(
        user_id=body.user_id,
        event_type="argonaut.mode_changed",
        entity_type="argonaut",
        entity_id=body.user_id,
        payload={"prior": prior, "new": body.mode},
    )
    try:
        await publish_event(
            "argonaut.mode_changed",
            {"user_id": body.user_id, "prior": prior, "new": body.mode},
        )
    except Exception:  # pragma: no cover - defensive
        pass
    return ModeResponse(
        status="ok", mode=body.mode, message=f"Mode {prior} -> {body.mode}"
    )


@router.post("/snapshot", response_model=ArgonautSnapshotDTO)
async def force_snapshot(body: ModeRequest | None = None) -> ArgonautSnapshotDTO:
    """Force a fresh daily snapshot for today. `body` is optional; only
    `user_id` is read.
    """
    user_id = body.user_id if body else "ariel"
    settings = load_agent_settings(user_id)
    acct = ArgonautAccount(user_id=user_id, settings=settings)
    payload = await acct.persist_daily_snapshot()
    return ArgonautSnapshotDTO(
        date=payload.date,
        total_value_usd=payload.total_value_usd,
        cash_usd=payload.cash_usd,
        positions_value_usd=payload.positions_value_usd,
        day_pnl_usd=payload.day_pnl_usd,
    )


@router.get("/trades", response_model=TradesResponse)
async def argonaut_trades(
    user_id: str = Query("ariel"),
    limit: int = Query(50, ge=1, le=500),
) -> TradesResponse:
    """Recent trades (paper + live fills) for the limited account.

    The fills table doesn't carry an account_class column directly;
    Phase 5 returns the most-recent fills for the user (the UI surfaces
    paper vs live via the `paper` flag) and lets the user filter
    visually. Phase 6+ will tag fills with `account_id`.
    """
    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(FillRow)
                .where(FillRow.user_id == user_id)
                .order_by(FillRow.filled_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    return TradesResponse(
        rows=[
            {
                "id": r.id,
                "ticker": r.ticker,
                "action": r.action,
                "quantity": float(r.quantity),
                "price": float(r.price),
                "commission": float(r.commission),
                "filled_at": r.filled_at.isoformat(),
                "paper": bool(r.paper),
                "broker": r.broker,
                "broker_order_id": r.broker_order_id,
            }
            for r in rows
        ]
    )


__all__ = ["router"]
