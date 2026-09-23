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
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, or_, select

from argosy.adapters.brokers.types import Fill as FillModel
from argosy.adapters.brokers.types import OrderSnapshot
from argosy.execution.audit import record_audit_event
from argosy.execution.fill_evidence import (
    custody_broker,
    ledger_amount,
    number,
    reconcile_fill_evidence,
)
from argosy.execution.settlement import MANUAL_RECEIPT_BROKERS
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import (
    Fill as FillRow,
)
from argosy.state.models import (
    FillBookApplication,
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
        self.adapter_factory = adapter_factory or (lambda broker: _default_factory(broker, user_id=self.user_id))

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict[str, int]:
        moment = (now or _utcnow)()
        summary = {
            "pending": 0,
            "fills_recorded": 0,
            "fills_deduped": 0,
            "terminal": 0,
            "errors": 0,
        }
        adapters: dict[str, Any] = {}
        async with AsyncExitStack() as cleanup, db_mod.get_session() as session:
            stmt = select(PendingOrder).where(
                PendingOrder.user_id == self.user_id,
                PendingOrder.broker.not_in(MANUAL_RECEIPT_BROKERS),
                or_(PendingOrder.status.in_(list(_OPEN_STATUSES)),
                    (PendingOrder.status.in_(["filled", "cancelled", "rejected"])
                     & or_(PendingOrder.last_polled_at.is_(None), PendingOrder.last_polled_at < moment - timedelta(hours=6))
                     & (PendingOrder.receipt_sync_error.is_not(None) | exists(select(FillRow.id).outerjoin(
                        FillBookApplication, FillBookApplication.fill_id == FillRow.id).where(
                        FillRow.proposal_id == PendingOrder.proposal_id,
                        FillRow.user_id == self.user_id, FillRow.paper.is_(False),
                        or_(FillBookApplication.settlement_json.is_(None), FillRow.execution_time_confirmed.is_(False),
                            FillRow.commission_confirmed.is_(False),
                            FillRow.price_currency.is_(None), func.trim(FillRow.price_currency) == "",
                            FillRow.commission_currency.is_(None), func.trim(FillRow.commission_currency) == ""),
                    ))))),
            )
            pending_rows = (await session.execute(stmt)).scalars().all()
            summary["pending"] = len(pending_rows)

            for po in pending_rows:
                po_id = po.id
                before_order = dict(summary)
                try:
                    async with session.begin_nested():
                        proposal_identity = await session.get(ProposalRow, po.proposal_id)
                        if (proposal_identity is None or proposal_identity.user_id != self.user_id
                                or proposal_identity.account_id != po.account_id
                                or custody_broker(po.account_id) != po.broker):
                            raise ValueError("pending order ownership/custody/broker contradicts its proposal")
                        if po.broker not in adapters:
                            adapters[po.broker] = self.adapter_factory(po.broker)
                            disconnect = getattr(adapters[po.broker], "disconnect", None)
                            if callable(disconnect):
                                cleanup.push_async_callback(_close_adapter, disconnect)
                        adapter = adapters[po.broker]
                        if adapter is None:
                            po.last_polled_at = moment
                            po.receipt_sync_error = "broker adapter unavailable"
                            summary["errors"] += 1
                            continue
                        try:
                            snapshot = await _query_order(adapter, po)
                        except Exception as exc:
                            summary["errors"] += 1
                            _log.exception("reconcile.query_failed", po_id=po.id)
                            po.last_polled_at = moment
                            po.receipt_sync_error = str(exc)
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
                            po.receipt_sync_error = "broker order snapshot unavailable"
                            continue
                        if snapshot.status == "unknown":
                            po.receipt_sync_error = "broker order state unknown"

                        # Persist new fills idempotently. A broker repeats prior partial
                        # executions on every poll; only new execution ids become rows.
                        for f in snapshot.fills:
                            if f.broker != po.broker or f.broker_order_id != po.broker_order_id:
                                raise ValueError("Broker snapshot receipt belongs to a different order")
                            inserted = await persist_broker_fill(
                                session,
                                user_id=self.user_id,
                                proposal_id=po.proposal_id,
                                account_id=po.account_id,
                                fill=f,
                                apply_to_book=False,
                            )
                            summary["fills_recorded" if inserted else "fills_deduped"] += 1


                        if snapshot.fills or snapshot.status in {"filled", "partial"} or snapshot.filled_quantity is not None:
                            proposal = await session.get(ProposalRow, po.proposal_id)
                            receipt_rows = (await session.scalars(select(FillRow).where(
                                FillRow.proposal_id == po.proposal_id,
                            ))).all()
                            evidence = reconcile_fill_evidence(proposal, list(receipt_rows)) if proposal else None
                            reported = number(snapshot.filled_quantity) if snapshot.filled_quantity is not None else None
                            count_matches = evidence is not None and reported is not None and reported >= 0 and reported == evidence.quantity
                            invalid = (evidence is None or bool(evidence.errors)
                                       or (snapshot.status == "filled" and not evidence.complete)
                                       or (snapshot.status == "partial" and not evidence.live_rows)
                                       or (reported is not None and not count_matches))
                            if invalid:
                                summary["errors"] += 1
                                po.receipt_sync_error = "broker execution status lacks complete matching receipts"
                                if snapshot.status == "partial":
                                    po.status = "partial"
                                await record_audit_event(
                                    user_id=self.user_id,
                                    event_type="reconcile.receipts_incomplete" if snapshot.fills else "reconcile.fill_details_missing",
                                    entity_type="pending_order", entity_id=str(po.id),
                                    payload={"reason": po.receipt_sync_error, "reported_filled_quantity": snapshot.filled_quantity,
                                             "errors": evidence.errors if evidence else ["missing proposal"]},
                                    session=session,
                                )
                                continue
                            # A prior completeness failure needs complete target
                            # receipts or an independent broker cumulative count.
                            # A cancelled/rejected status alone cannot clear it.
                            if evidence.complete or count_matches:
                                po.receipt_sync_error = None

                        if snapshot.status in ("filled", "partial", "cancelled", "rejected"):
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
                except Exception as exc:
                    summary.update(before_order)
                    summary["errors"] += 1
                    await record_audit_event(
                        user_id=self.user_id, event_type="reconcile.order_failed",
                        entity_type="pending_order", entity_id=str(po_id),
                        payload={"error": f"{type(exc).__name__}: {exc}"}, session=session,
                    )
                    failed = await session.get(PendingOrder, po_id)
                    failed.last_polled_at = moment
                    failed.receipt_sync_error = f"{type(exc).__name__}: {exc}"
            # All polled orders are staged before any shares/cash application.
            # Broker response/order enumeration must not determine chronology.
            from argosy.execution.fill_book import recover_fill_applications

            await session.flush()
            book = await session.run_sync(lambda db: recover_fill_applications(
                db, user_id=self.user_id, commit=False))
            summary["book_applied"] = book["applied"]
            summary["errors"] += book["errors"]
            await session.commit()
        summary["error_count"] = summary["errors"]
        return summary


# ----------------------------------------------------------------------
# Adapter query
# ----------------------------------------------------------------------


_OrderSnapshot = OrderSnapshot


async def _close_adapter(disconnect) -> None:
    await _maybe_async(disconnect())


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
    apply_to_book: bool = True,
) -> bool:
    """Persist one live execution exactly once with audit/verdict lineage."""
    if fill.paper:
        raise ValueError("Paper execution cannot be recorded as a live broker fill")
    if fill.proposal_id is not None and fill.proposal_id != proposal_id:
        raise ValueError("Broker receipt names a different proposal")
    if account_id and fill.account_id and account_id != fill.account_id:
        raise ValueError("Broker receipt custody differs from the order account")
    resolved_account = account_id or fill.account_id
    expected_broker = custody_broker(resolved_account)
    if expected_broker and fill.broker != expected_broker:
        raise ValueError("Broker receipt broker differs from custody")
    if number(fill.quantity) <= 0 or number(fill.price) <= 0:
        raise ValueError("Broker receipt quantity and price must be positive")
    number(fill.commission)
    quantity, price = ledger_amount(fill.quantity), ledger_amount(fill.price)
    commission = ledger_amount(fill.commission, positive=False)
    if proposal_id is not None:
        proposal = await session.get(ProposalRow, proposal_id)
        if proposal is None or proposal.user_id != user_id:
            raise ValueError("Broker receipt proposal ownership is unproven")
        if proposal.ticker != fill.ticker or proposal.action != fill.action:
            raise ValueError("Broker receipt ticker/side differs from its proposal")
        if proposal.account_id and proposal.account_id != resolved_account:
            raise ValueError("Broker receipt custody differs from its proposal")
    from argosy.services.chat_advisor.execution_policy import (
        assert_proposal_can_mutate,
    )

    await assert_proposal_can_mutate(
        session, proposal_id, operation="fill recording"
    )
    identity = _fill_identity(fill)
    time_confirmed = (fill.execution_time_confirmed if fill.execution_time_confirmed is not None
                      else "filled_at" in fill.model_fields_set)
    if time_confirmed and (fill.filled_at.tzinfo is None or fill.filled_at.utcoffset() is None):
        raise ValueError("confirmed execution timestamp requires an explicit timezone")
    execution_time = _aware_utc(fill.filled_at)
    existing = (
        await session.execute(
            select(FillRow).where(
                FillRow.user_id == user_id,
                FillRow.broker == fill.broker,
                FillRow.external_fill_id == identity,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        expected = {"proposal_id": proposal_id, "account_id": resolved_account,
                    "ticker": fill.ticker, "action": fill.action, "broker_order_id": fill.broker_order_id,
                    "paper": False}
        if (any(getattr(existing, key) != value for key, value in expected.items())
                or any(number(getattr(existing, key)) != value
                       for key, value in (("quantity", quantity), ("price", price)))
                or (time_confirmed and existing.execution_time_confirmed
                    and _aware_utc(existing.filled_at) != _aware_utc(fill.filled_at))):
            raise ValueError("Broker execution identity already exists with different receipt facts")
        from argosy.execution.fill_book import (
            apply_received_fill,
            confirm_broker_metadata,
            confirm_execution_time,
        )

        await session.run_sync(lambda db: confirm_broker_metadata(db, existing,
            commission=commission if fill.commission_confirmed else None, native_account_id=fill.native_account_id,
            price_currency=fill.price_currency, commission_currency=fill.commission_currency))
        if time_confirmed:
            await session.run_sync(lambda db: confirm_execution_time(db, existing, execution_time))
        await session.run_sync(lambda db: apply_received_fill(
            db, fill_id=existing.id, user_id=user_id, settlement=fill.settlement, defer_application=not apply_to_book))
        await _sync_order_sheet_fill_telemetry(session, user_id=user_id, proposal_id=proposal_id)
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
        quantity=quantity,
        price=price,
        commission=commission,
        filled_at=execution_time,
        paper=False,
        execution_time_confirmed=time_confirmed,
        commission_confirmed=fill.commission_confirmed,
        native_account_id=fill.native_account_id,
        price_currency=fill.price_currency,
        commission_currency=fill.commission_currency,
    )
    session.add(row)
    await session.flush()
    from argosy.execution.fill_book import apply_received_fill

    await session.run_sync(lambda db: apply_received_fill(
        db, fill_id=row.id, user_id=user_id, settlement=fill.settlement, defer_application=not apply_to_book))
    prediction_synced = await _sync_order_sheet_fill_telemetry(
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
            "commission_confirmed": fill.commission_confirmed,
            "native_account_id": fill.native_account_id,
            "prediction_telemetry_synced": prediction_synced,
        },
        session=session,
    )
    return True


async def _sync_order_sheet_fill_telemetry(
    session: Any,
    *,
    user_id: str,
    proposal_id: int | None,
) -> bool:
    """Attach execution facts without rewriting the recommendation's clock.

    Surfacing may precede executable Proposal creation; the old proposal_id
    source reference can name an ActionProposal instead. Only immutable
    sheet fingerprint + ticker + authored action joins these two lifecycles.
    """
    if proposal_id is None:
        return False
    from argosy.state.models import Prediction

    proposal = await session.get(ProposalRow, proposal_id)
    if proposal is None or proposal.user_id != user_id:
        return False
    try:
        authored = json.loads(proposal.expected_impact_json or "{}")
        fingerprint = authored["order_sheet_fingerprint"]
        action = authored["order_line"]["action"]
        if not isinstance(fingerprint, str) or not fingerprint:
            return False
    except (KeyError, TypeError, ValueError):
        return False  # Ambiguous legacy ids are not evidence of linkage.

    predictions = (
        await session.execute(
            select(Prediction).where(
                Prediction.user_id == user_id,
                Prediction.source == "signal_stream:order_sheet",
            )
        )
    ).scalars().all()
    linked = []
    for candidate in predictions:
        try:
            parsed = json.loads(candidate.source_ref or "{}")
        except (TypeError, ValueError):
            continue
        if (isinstance(parsed, dict) and parsed.get("order_sheet_fingerprint") == fingerprint
                and candidate.ticker == proposal.ticker and parsed.get("action") == action):
            linked.append((candidate, parsed))
    if not linked:
        return False
    fills = (
        await session.execute(
            select(FillRow).where(
                FillRow.proposal_id == proposal_id,
            )
        )
    ).scalars().all()
    evidence = reconcile_fill_evidence(proposal, list(fills))
    if not evidence.live_rows and not evidence.errors:
        return False
    fills = evidence.live_rows
    times_confirmed = bool(fills) and all(row.execution_time_confirmed for row in fills)
    first_fill = min((_aware_utc(row.filled_at) for row in fills), default=None) if times_confirmed else None
    last_fill = max((_aware_utc(row.filled_at) for row in fills), default=None) if times_confirmed else None
    telemetry = {
        "execution_proposal_id": proposal_id,
        "status": "reconciliation_required" if evidence.errors else "verified" if (
            times_confirmed and evidence.commission_confirmed and evidence.price_currency
            and evidence.price_currency == evidence.commission_currency) else "execution_details_incomplete",
        "receipt_errors": evidence.errors,
        "complete": evidence.complete,
        "quantity": float(evidence.quantity),
        "vwap": round(evidence.vwap, 4) if evidence.vwap is not None else None,
        "commission": float(evidence.commission) if evidence.commission_confirmed and evidence.commission_currency else None,
        "price_currency": evidence.price_currency,
        "commission_currency": evidence.commission_currency,
        "commission_confirmed": evidence.commission_confirmed,
        "execution_time_confirmed": times_confirmed,
        "price_currencies": sorted({row.price_currency for row in fills if row.price_currency}),
        "commission_currencies": sorted({row.commission_currency for row in fills if row.commission_currency}),
        "first_fill_at": first_fill.isoformat() if first_fill else None,
        "last_fill_at": last_fill.isoformat() if last_fill else None,
        "external_fill_ids": sorted(
            row.external_fill_id for row in fills if row.external_fill_id
        ),
    }
    for prediction, source_ref in linked:
        source_ref["fill_telemetry"] = telemetry
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


def _default_factory(broker: str, *, user_id: str) -> Any:
    """Default adapter factory used when caller doesn't inject one."""
    if broker == "ibkr":
        from argosy.adapters.brokers.ibkr import IBKRAdapter

        return IBKRAdapter(user_id=user_id)
    return None


__all__ = [
    "ReconcileLoop",
    "fills_for_verdict",
    "persist_broker_fill",
    "verdict_for_fill",
]
