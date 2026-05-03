"""Append-only audit_log helpers (SDD §14.1).

Two public helpers:

  - `record_audit_event(...)` — write one `audit_log` row. Single source
    of truth for *every* audit-worthy event in Phase 4+.
  - `write_paper_fill(...)`   — convenience wrapper that records both a
    `fills` row (paper=True) AND a corresponding audit_log entry. Every
    broker adapter calls this when `paper=True`.

Both helpers accept an optional `session` so callers can compose them
inside an existing transaction. If `session` is None, a fresh session is
opened and the row is committed immediately.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from argosy.adapters.brokers.types import Fill as FillModel
from argosy.state import db as db_mod
from argosy.state.models import AuditLog, Fill as FillRow


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def record_audit_event(
    *,
    user_id: str,
    event_type: str,
    entity_type: str = "",
    entity_id: str = "",
    payload: dict[str, Any] | None = None,
    session: AsyncSession | None = None,
) -> int:
    """Append one row to `audit_log`. Returns the new row's id.

    `payload` is JSON-serialized (datetimes coerced via `default=str`).
    If a `session` is supplied the caller owns its transaction; otherwise
    we open a session and commit immediately.
    """
    payload_json = ""
    if payload is not None:
        try:
            payload_json = json.dumps(payload, default=str)
        except (TypeError, ValueError):
            payload_json = json.dumps({"_unserializable": str(payload)})

    row = AuditLog(
        user_id=user_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else "",
        payload_json=payload_json,
        created_at=_utcnow(),
    )

    if session is not None:
        session.add(row)
        await session.flush()
        return int(row.id)

    async with db_mod.get_session() as fresh:
        fresh.add(row)
        await fresh.commit()
        await fresh.refresh(row)
        return int(row.id)


async def write_paper_fill(
    *,
    user_id: str,
    broker: str,
    ticker: str,
    action: str,
    quantity: float,
    price: float,
    proposal_id: int | None = None,
    broker_order_id: str = "",
    commission: float = 0.0,
    session: AsyncSession | None = None,
) -> FillModel:
    """Record a PaperFill in both `fills` and `audit_log`.

    Symmetric with the live path: same column shape, `paper=True`. Tests
    rely on the dual-row write, so adapters MUST go through this helper
    for paper-mode placements.
    """
    moment = _utcnow()
    row = FillRow(
        user_id=user_id,
        proposal_id=proposal_id,
        broker=broker,
        broker_order_id=broker_order_id,
        ticker=ticker,
        action=action,
        quantity=quantity,
        price=price,
        commission=commission,
        filled_at=moment,
        paper=True,
    )
    payload = {
        "broker": broker,
        "ticker": ticker,
        "action": action,
        "quantity": quantity,
        "price": price,
        "commission": commission,
        "broker_order_id": broker_order_id,
        "proposal_id": proposal_id,
        "paper": True,
    }

    if session is not None:
        session.add(row)
        await session.flush()
        await record_audit_event(
            user_id=user_id,
            event_type="paper_fill.recorded",
            entity_type="proposal" if proposal_id else "order",
            entity_id=str(proposal_id) if proposal_id else broker_order_id,
            payload=payload,
            session=session,
        )
    else:
        async with db_mod.get_session() as fresh:
            fresh.add(row)
            await fresh.flush()
            await record_audit_event(
                user_id=user_id,
                event_type="paper_fill.recorded",
                entity_type="proposal" if proposal_id else "order",
                entity_id=str(proposal_id) if proposal_id else broker_order_id,
                payload=payload,
                session=fresh,
            )
            await fresh.commit()
            await fresh.refresh(row)

    return FillModel(
        proposal_id=proposal_id,
        broker=broker,
        broker_order_id=broker_order_id,
        ticker=ticker,
        action=action,  # type: ignore[arg-type]
        quantity=quantity,
        price=price,
        commission=commission,
        filled_at=moment,
        paper=True,
    )


__all__ = ["record_audit_event", "write_paper_fill"]
