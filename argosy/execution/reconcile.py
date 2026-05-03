"""Reconcile loop (SDD §10.5, §10.6, Phase 4).

Polls `pending_orders` every 30s during market hours. For each open
order, asks the broker adapter for the order status and any new fills.
Updates `fills` rows and advances the proposal as needed.

State transitions:

  - "filled"   → write Fill row(s); leave proposal at EXECUTED_LIVE
  - "partial"  → write Fill row(s); pending_orders.status = "partial"
  - "cancelled" → record audit event; pending_orders.status = "cancelled"
  - "rejected" → record audit event; pending_orders.status = "rejected"

Tests inject mock adapters via `adapter_factory`. Real wiring imports
the IBKRAdapter at runtime.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select

from argosy.adapters.brokers.types import Fill as FillModel
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import (
    Fill as FillRow,
    PendingOrder,
    Proposal as ProposalRow,
    ProposalHistory,
)

_log = get_logger("argosy.execution.reconcile")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Statuses we still poll
_OPEN_STATUSES = {"submitted", "partial", "working", "presubmitted"}


class ReconcileLoop(CadenceLoop):
    """30s cadence: walk pending_orders and reconcile fills."""

    name = "reconcile"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        adapter_factory: Callable[[str], Any] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule or LoopSchedule(interval_seconds=30, market_hours_only=True),
            enabled=enabled,
        )
        self.user_id = user_id
        self.adapter_factory = adapter_factory or _default_factory

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        moment = (now or _utcnow)()
        async with db_mod.get_session() as session:
            stmt = select(PendingOrder).where(
                PendingOrder.user_id == self.user_id,
                PendingOrder.status.in_(list(_OPEN_STATUSES)),
            )
            pending_rows = (await session.execute(stmt)).scalars().all()

            for po in pending_rows:
                adapter = self.adapter_factory(po.broker)
                if adapter is None:
                    continue
                try:
                    snapshot = await _query_order(adapter, po)
                except Exception as exc:
                    _log.exception("reconcile.query_failed", po_id=po.id)
                    po.last_polled_at = moment
                    await record_audit_event(
                        user_id=self.user_id,
                        event_type="reconcile.query_failed",
                        entity_type="pending_order",
                        entity_id=str(po.id),
                        payload={"error": str(exc)},
                        session=session,
                    )
                    continue

                po.last_polled_at = moment
                if snapshot is None:
                    continue

                # Persist new fills.
                for f in snapshot.fills:
                    session.add(
                        FillRow(
                            user_id=self.user_id,
                            proposal_id=po.proposal_id,
                            broker=po.broker,
                            broker_order_id=po.broker_order_id,
                            ticker=f.ticker,
                            action=f.action,
                            quantity=f.quantity,
                            price=f.price,
                            commission=f.commission,
                            filled_at=f.filled_at,
                            paper=False,
                        )
                    )
                    await record_audit_event(
                        user_id=self.user_id,
                        event_type="fill.received",
                        entity_type="proposal",
                        entity_id=str(po.proposal_id),
                        payload={
                            "broker": po.broker,
                            "broker_order_id": po.broker_order_id,
                            "ticker": f.ticker,
                            "action": f.action,
                            "quantity": f.quantity,
                            "price": f.price,
                            "commission": f.commission,
                        },
                        session=session,
                    )

                if snapshot.status in ("filled", "partial", "cancelled", "rejected"):
                    po.status = snapshot.status
                    po.updated_at = moment

                    if snapshot.status in ("cancelled", "rejected"):
                        # Append a history breadcrumb. Proposal stays
                        # EXECUTED_LIVE (terminal) — the broker outcome is
                        # captured in audit_log + pending_orders.status.
                        proposal = await session.get(ProposalRow, po.proposal_id)
                        proposal_status = (
                            proposal.status if proposal is not None else "executed_live"
                        )
                        session.add(
                            ProposalHistory(
                                proposal_id=po.proposal_id,
                                status=proposal_status,
                                transitioned_at=moment,
                                transitioned_by="reconcile_loop",
                                note=f"broker reported {snapshot.status}: {snapshot.reason}",
                            )
                        )
                        await record_audit_event(
                            user_id=self.user_id,
                            event_type=f"order.{snapshot.status}",
                            entity_type="proposal",
                            entity_id=str(po.proposal_id),
                            payload={
                                "broker": po.broker,
                                "broker_order_id": po.broker_order_id,
                                "reason": snapshot.reason,
                            },
                            session=session,
                        )

            await session.commit()


# ----------------------------------------------------------------------
# Adapter query
# ----------------------------------------------------------------------


class _OrderSnapshot:
    __slots__ = ("status", "fills", "reason")

    def __init__(
        self,
        status: str,
        fills: list[FillModel],
        reason: str = "",
    ) -> None:
        self.status = status
        self.fills = fills
        self.reason = reason


async def _query_order(adapter: Any, po: PendingOrder) -> _OrderSnapshot | None:
    """Ask the adapter for an order snapshot.

    The Protocol doesn't define a "get one order" method (open_orders is
    list-based), so adapters that want richer reconcile semantics can
    expose `get_order_snapshot(broker_order_id)`. If absent, we fall back
    to scanning `get_open_orders` and inferring a snapshot.
    """
    custom = getattr(adapter, "get_order_snapshot", None)
    if callable(custom):
        return await _maybe_async(custom(po.broker_order_id))

    open_orders = adapter.get_open_orders("limited") if hasattr(adapter, "get_open_orders") else []
    found = next(
        (o for o in (open_orders or []) if o.broker_order_id == po.broker_order_id),
        None,
    )
    if found is None:
        # Order no longer open — assume filled.
        return _OrderSnapshot(status="filled", fills=[], reason="absent from open orders")
    if found.filled_quantity and found.filled_quantity < found.quantity:
        return _OrderSnapshot(status="partial", fills=[], reason="partial fill in progress")
    return None


async def _maybe_async(maybe_coro: Any) -> Any:
    """Await if it's a coroutine; else return as-is."""
    import inspect

    if inspect.iscoroutine(maybe_coro):
        return await maybe_coro
    return maybe_coro


def _default_factory(broker: str) -> Any:
    """Default adapter factory used when caller doesn't inject one."""
    if broker == "ibkr":
        from argosy.adapters.brokers.ibkr import IBKRAdapter

        return IBKRAdapter(user_id="ariel")
    return None


__all__ = ["ReconcileLoop"]
