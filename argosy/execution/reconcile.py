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

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from argosy.adapters.brokers.types import Fill as FillModel
from argosy.adapters.brokers.types import OrderSnapshot
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import (
    Fill as FillRow,
)
from argosy.state.models import (
    PendingOrder,
    ProposalHistory,
)
from argosy.state.models import (
    Proposal as ProposalRow,
)
from argosy.state.models import (
    Verdict as VerdictRow,
)

_log = get_logger("argosy.execution.reconcile")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    """Normalize SQLite-naive and broker-aware timestamps for comparison."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict[str, int]:
        moment = (now or _utcnow)()
        summary = {
            "pending": 0,
            "fills_recorded": 0,
            "fills_deduped": 0,
            "terminal": 0,
            "errors": 0,
        }
        async with db_mod.get_session() as session:
            stmt = select(PendingOrder).where(
                PendingOrder.user_id == self.user_id,
                PendingOrder.status.in_(list(_OPEN_STATUSES)),
            )
            pending_rows = (await session.execute(stmt)).scalars().all()
            summary["pending"] = len(pending_rows)

            for po in pending_rows:
                adapter = self.adapter_factory(po.broker)
                if adapter is None:
                    summary["errors"] += 1
                    continue
                try:
                    snapshot = await _query_order(adapter, po)
                except Exception as exc:
                    summary["errors"] += 1
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

                # Persist new fills idempotently. A broker repeats prior partial
                # executions on every poll; only new execution ids become rows.
                for f in snapshot.fills:
                    inserted = await persist_broker_fill(
                        session,
                        user_id=self.user_id,
                        proposal_id=po.proposal_id,
                        account_id=po.account_id,
                        fill=f,
                    )
                    summary["fills_recorded" if inserted else "fills_deduped"] += 1

                if snapshot.status in ("filled", "partial", "cancelled", "rejected"):
                    if snapshot.status == "filled" and not snapshot.fills:
                        summary["errors"] += 1
                        await record_audit_event(
                            user_id=self.user_id,
                            event_type="reconcile.fill_details_missing",
                            entity_type="pending_order",
                            entity_id=str(po.id),
                            payload={
                                "broker": po.broker,
                                "broker_order_id": po.broker_order_id,
                                "reason": snapshot.reason,
                            },
                            session=session,
                        )
                        continue
                    po.status = snapshot.status
                    po.updated_at = moment
                    if snapshot.status in ("filled", "cancelled", "rejected"):
                        summary["terminal"] += 1

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
        return summary


# ----------------------------------------------------------------------
# Adapter query
# ----------------------------------------------------------------------


_OrderSnapshot = OrderSnapshot


async def _query_order(adapter: Any, po: PendingOrder) -> OrderSnapshot | None:
    """Ask the adapter for an order snapshot.

    The Protocol doesn't define a "get one order" method (open_orders is
    list-based), so adapters that want richer reconcile semantics can
    expose `get_order_snapshot(broker_order_id)`. If absent, we fall back
    to scanning `get_open_orders` and inferring a snapshot.
    """
    custom = getattr(adapter, "get_order_snapshot", None)
    if callable(custom):
        try:
            return await _maybe_async(
                custom(po.broker_order_id, account_id=po.account_id)
            )
        except TypeError:
            return await _maybe_async(custom(po.broker_order_id))

    if not po.account_id:
        return OrderSnapshot(
            status="unknown", fills=[], reason="pending order has no account_id"
        )
    open_orders = adapter.get_open_orders(po.account_id) if hasattr(adapter, "get_open_orders") else []
    found = next(
        (o for o in (open_orders or []) if o.broker_order_id == po.broker_order_id),
        None,
    )
    if found is None:
        # Absence from the open list is not proof of a fill: the order may be
        # cancelled, rejected, expired, or outside the current broker session.
        return OrderSnapshot(
            status="unknown", fills=[], reason="absent from open orders"
        )
    if found.filled_quantity and found.filled_quantity < found.quantity:
        return _OrderSnapshot(status="partial", fills=[], reason="partial fill in progress")
    return None


async def _maybe_async(maybe_coro: Any) -> Any:
    """Await if it's a coroutine; else return as-is."""
    import inspect

    if inspect.iscoroutine(maybe_coro):
        return await maybe_coro
    return maybe_coro


def _fill_identity(fill: FillModel) -> str:
    if fill.external_fill_id:
        return fill.external_fill_id
    raw = "|".join(
        (
            fill.broker,
            fill.broker_order_id,
            fill.ticker.upper(),
            fill.action.lower(),
            f"{float(fill.quantity):.8f}",
            f"{float(fill.price):.8f}",
            fill.filled_at.isoformat(),
        )
    )
    return "derived:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


async def persist_broker_fill(
    session: Any,
    *,
    user_id: str,
    proposal_id: int | None,
    account_id: str,
    fill: FillModel,
) -> bool:
    """Persist one live execution exactly once with audit/verdict lineage."""
    identity = _fill_identity(fill)
    existing = (
        await session.execute(
            select(FillRow.id).where(
                FillRow.user_id == user_id,
                FillRow.broker == fill.broker,
                FillRow.external_fill_id == identity,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False
    try:
        verdict_id = await _resolve_verdict_id(
            session,
            user_id=user_id,
            proposal_id=proposal_id,
            ticker=fill.ticker,
        )
    except Exception:  # noqa: BLE001
        _log.warning("reconcile.verdict_resolve_raised", exc_info=True)
        verdict_id = None
    resolved_account = account_id or fill.account_id
    row = FillRow(
        user_id=user_id,
        proposal_id=proposal_id,
        verdict_id=verdict_id,
        broker=fill.broker,
        broker_order_id=fill.broker_order_id,
        external_fill_id=identity,
        account_id=resolved_account,
        ticker=fill.ticker,
        action=fill.action,
        quantity=fill.quantity,
        price=fill.price,
        commission=fill.commission,
        filled_at=fill.filled_at,
        paper=False,
    )
    session.add(row)
    await session.flush()
    prediction_synced = await _sync_order_sheet_prediction_entry(
        session, user_id=user_id, proposal_id=proposal_id
    )
    await record_audit_event(
        user_id=user_id,
        event_type="fill.received",
        entity_type="proposal",
        entity_id=str(proposal_id or ""),
        payload={
            "broker": fill.broker,
            "broker_order_id": fill.broker_order_id,
            "external_fill_id": identity,
            "account_id": resolved_account,
            "ticker": fill.ticker,
            "action": fill.action,
            "quantity": fill.quantity,
            "price": fill.price,
            "commission": fill.commission,
            "prediction_entry_synced": prediction_synced,
        },
        session=session,
    )
    return True


async def _sync_order_sheet_prediction_entry(
    session: Any,
    *,
    user_id: str,
    proposal_id: int | None,
) -> bool:
    """Replace quote-time prediction entry with actual fill VWAP.

    This is the operational bridge from fill telemetry to due-date outcome
    scoring. It never mutates an already-evaluated prediction.
    """
    if proposal_id is None:
        return False
    from argosy.state.models import Prediction, PredictionOutcome

    predictions = (
        await session.execute(
            select(Prediction).where(
                Prediction.user_id == user_id,
                Prediction.source == "signal_stream:order_sheet",
            )
        )
    ).scalars().all()
    prediction = None
    source_ref: dict[str, Any] = {}
    for candidate in predictions:
        try:
            parsed = json.loads(candidate.source_ref or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed.get("proposal_id") == proposal_id:
            prediction = candidate
            source_ref = parsed
            break
    if prediction is None:
        return False
    has_outcome = (
        await session.execute(
            select(PredictionOutcome.id)
            .where(PredictionOutcome.prediction_id == prediction.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if has_outcome is not None:
        return False
    fills = (
        await session.execute(
            select(FillRow).where(
                FillRow.user_id == user_id,
                FillRow.proposal_id == proposal_id,
                FillRow.paper.is_(False),
            )
        )
    ).scalars().all()
    if not fills:
        return False
    total_qty = sum(float(row.quantity) for row in fills)
    if total_qty <= 0:
        return False
    vwap = sum(float(row.quantity) * float(row.price) for row in fills) / total_qty
    first_fill = min(_aware_utc(row.filled_at) for row in fills)
    last_fill = max(_aware_utc(row.filled_at) for row in fills)
    total_commission = sum(float(row.commission) for row in fills)
    prediction.entry_price = Decimal(str(round(vwap, 4)))
    prediction.event_at = first_fill
    source_ref["fill_telemetry"] = {
        "quantity": round(total_qty, 8),
        "vwap": round(vwap, 4),
        "commission": round(total_commission, 4),
        "first_fill_at": first_fill.isoformat(),
        "last_fill_at": last_fill.isoformat(),
        "external_fill_ids": sorted(
            row.external_fill_id for row in fills if row.external_fill_id
        ),
    }
    prediction.source_ref = json.dumps(source_ref, sort_keys=True)
    return True


# ----------------------------------------------------------------------
# Seam 4: fill ↔ verdict linkage (best-effort resolve + read helpers)
# ----------------------------------------------------------------------


async def _resolve_verdict_id(
    session: Any,
    *,
    user_id: str,
    proposal_id: int | None,
    ticker: str,
) -> int | None:
    """Resolve the settled verdict that recommended a fill.

    Walks ``fills.proposal_id → proposals.decision_run_id`` then finds the
    settled ``verdicts`` row with matching ``source_decision_run_id`` and the
    same subject (ticker) + user. Returns the verdict id, or ``None`` when
    nothing is resolvable (no proposal, no decision_run_id, no settled
    verdict, blank ticker).

    This is a MONEY-PATH best-effort helper: it NEVER raises. Any failure is
    logged and swallowed so a resolution error can never break a fill write.
    """
    try:
        if proposal_id is None:
            return None
        subject = (ticker or "").strip().upper()
        if not subject:
            return None
        # READ-ONLY under no_autoflush: the caller has already dirtied
        # pending_order.last_polled_at, and a plain session.get()/execute()
        # would AUTOFLUSH that dirty row. Sol review: if that autoflush fails,
        # the session flips rollback-only and the subsequent fill commit raises
        # PendingRollbackError — DROPPING the fill. Disabling autoflush isolates
        # this resolution from the fill-write transaction: no flush is triggered,
        # so a resolution failure can never poison the fill write.
        with session.no_autoflush:
            proposal = await session.get(ProposalRow, proposal_id)
            if proposal is None or proposal.decision_run_id is None:
                return None
            # NO settled filter: supersession sets the prior verdict's
            # settled=False, but a fill from that still-live proposal was still
            # recommended by that run's verdict. Match the verdict OWNED BY the
            # run (source_decision_run_id) regardless of current settled state
            # (subject + user still guarded); take the latest by id on ties.
            return (
                await session.execute(
                    select(VerdictRow.id)
                    .where(
                        VerdictRow.user_id == user_id,
                        VerdictRow.source_decision_run_id == proposal.decision_run_id,
                        VerdictRow.subject == subject,
                    )
                    .order_by(VerdictRow.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — best-effort; must not break the fill write
        _log.warning(
            "reconcile.verdict_resolve_failed",
            proposal_id=proposal_id,
            ticker=ticker,
            exc_info=True,
        )
        return None


async def fills_for_verdict(session: Any, *, verdict_id: int) -> list[FillRow]:
    """All fills linked to ``verdict_id`` (oldest first).

    The read side of seam 4 — a future "did the user act on verdict V?"
    surface asks this. Returns [] when the verdict has no linked fills.
    """
    if verdict_id is None:
        return []
    rows = (
        await session.execute(
            select(FillRow)
            .where(FillRow.verdict_id == verdict_id)
            .order_by(FillRow.id)
        )
    ).scalars().all()
    return list(rows)


async def verdict_for_fill(session: Any, fill: FillRow) -> VerdictRow | None:
    """The verdict a ``fill`` traces back to, or ``None`` if unlinked."""
    if fill is None or fill.verdict_id is None:
        return None
    return await session.get(VerdictRow, fill.verdict_id)


def _default_factory(broker: str) -> Any:
    """Default adapter factory used when caller doesn't inject one."""
    if broker == "ibkr":
        from argosy.adapters.brokers.ibkr import IBKRAdapter

        return IBKRAdapter(user_id="ariel")
    return None


__all__ = [
    "ReconcileLoop",
    "fills_for_verdict",
    "persist_broker_fill",
    "verdict_for_fill",
]
