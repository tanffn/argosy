"""Trusted receipt path for orders placed at read-only brokers.

Schwab and Leumi adapters intentionally cannot place orders.  After the user
places an approved Argosy proposal in the broker UI, this service records the
broker's execution facts without letting request data redefine the approved
ticker, side, account, or target quantity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from math import isfinite

from sqlalchemy import select

from argosy.adapters.brokers.types import Fill as BrokerFill
from argosy.decisions.proposals import ProposalStatus, assert_legal
from argosy.execution.audit import record_audit_event
from argosy.execution.fill_evidence import ledger_amount, number, reconcile_fill_evidence
from argosy.execution.reconcile import _sync_order_sheet_fill_telemetry, persist_broker_fill
from argosy.execution.settlement import MANUAL_RECEIPT_BROKERS, FillSettlement
from argosy.state.models import Fill, PendingOrder, Proposal, ProposalHistory


@dataclass(frozen=True)
class ManualFillResult:
    fill_id: int
    created: bool
    proposal_id: int
    proposal_status: str
    pending_order_id: int
    pending_status: str
    broker: str
    account_id: str
    filled_quantity: float
    target_quantity: float
    book_status: str
    book_reason: str
    applied_snapshot_id: int | None


def _manual_broker(account_id: str) -> str:
    value = account_id.strip().lower()
    if value.startswith("schwab"):
        return "schwab_csv"
    if value.startswith("leumi"):
        return "leumi_tsv"
    raise ValueError(
        "manual fill capture is only for read-only Schwab or Leumi accounts"
    )


async def record_manual_fill(
    session,
    *,
    user_id: str,
    proposal_id: int,
    broker_order_id: str,
    external_fill_id: str,
    quantity: float,
    price: float,
    commission: float | None = None,
    filled_at: datetime | None = None,
    settlement: FillSettlement | None = None,
) -> ManualFillResult:
    """Record one broker-confirmed execution against an exact proposal.

    Partial fills are supported.  The first receipt moves the proposal from
    ``approved`` to ``executed_live``; its pending-order receipt stays
    ``partial`` until cumulative fill quantity reaches the approved quantity.
    Replaying the same broker execution id is idempotent.
    """
    if settlement is not None and filled_at is None:
        raise ValueError("broker execution timestamp is required with settlement facts")
    if filled_at is not None:
        if filled_at.tzinfo is None or filled_at.utcoffset() is None:
            raise ValueError("confirmed execution timestamp requires an explicit timezone")
        filled_at = filled_at.astimezone(UTC)
    proposal = await session.get(Proposal, proposal_id)
    if proposal is None:
        raise LookupError(f"proposal {proposal_id} not found")
    if proposal.user_id != user_id:
        raise PermissionError(
            f"proposal {proposal_id} belongs to {proposal.user_id}, not {user_id}"
        )
    status = ProposalStatus(proposal.status)
    if status not in (ProposalStatus.APPROVED, ProposalStatus.EXECUTED_LIVE):
        raise ValueError(
            f"proposal {proposal_id} must be approved before recording a fill; "
            f"current status is {status.value}"
        )
    if proposal.size_units != "shares":
        raise ValueError("manual fill capture requires a share-sized proposal")
    if proposal.action not in ("buy", "sell"):
        raise ValueError("manual fill capture requires a buy or sell proposal")

    broker = _manual_broker(proposal.account_id)
    order_id = broker_order_id.strip()
    execution_id = external_fill_id.strip()
    if not order_id:
        raise ValueError("broker_order_id is required")
    if not execution_id:
        raise ValueError("external_fill_id is required for idempotency")
    if execution_id.startswith("derived:"):
        raise ValueError("external_fill_id must be broker-issued, not a reserved derived identity")
    if not isfinite(quantity) or quantity <= 0:
        raise ValueError("quantity must be positive")
    if not isfinite(price) or price <= 0:
        raise ValueError("price must be positive")
    if commission is not None and (not isfinite(commission) or commission < 0):
        raise ValueError("commission cannot be negative")
    quantity_exact, price_exact = ledger_amount(quantity), ledger_amount(price)
    commission_exact = ledger_amount(commission, positive=False) if commission is not None else None

    existing = (
        await session.execute(
            select(Fill).where(
                Fill.user_id == user_id,
                Fill.broker == broker,
                Fill.external_fill_id == execution_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        evidence = reconcile_fill_evidence(proposal, [existing])
        if (
            existing.paper or evidence.errors
            or existing.ticker != proposal.ticker or existing.action != proposal.action
            or existing.account_id != proposal.account_id
            or existing.proposal_id != proposal_id
            or existing.broker_order_id != order_id
            or number(existing.quantity) != quantity_exact
            or number(existing.price) != price_exact
            or (commission_exact is not None and existing.commission_confirmed and number(existing.commission) != commission_exact)
            or (filled_at is not None and existing.execution_time_confirmed and
                existing.filled_at.replace(tzinfo=existing.filled_at.tzinfo or UTC).astimezone(UTC) !=
                filled_at.replace(tzinfo=filled_at.tzinfo or UTC).astimezone(UTC))
        ):
            raise ValueError(
                "external_fill_id already exists with different execution facts"
            )
        pending = (
            await session.execute(
                select(PendingOrder).where(
                    PendingOrder.user_id == user_id,
                    PendingOrder.proposal_id == proposal_id,
                    PendingOrder.broker == broker,
                    PendingOrder.broker_order_id == order_id,
                )
            )
        ).scalar_one()
        if pending.account_id != proposal.account_id:
            raise ValueError("pending order custody differs from the verified proposal")
        total = await _filled_quantity(session, user_id, proposal_id)
        await _resolve_manual_adapter_barrier(session, pending)
        from argosy.execution.fill_book import (
            apply_received_fill,
            confirm_broker_metadata,
            confirm_execution_time,
        )

        await session.run_sync(lambda db: confirm_broker_metadata(db, existing, commission=commission_exact,
            price_currency=settlement.currency if settlement else None,
            commission_currency=settlement.currency if settlement else None))
        if filled_at is not None:
            await session.run_sync(lambda db: confirm_execution_time(db, existing, filled_at))
        application = await session.run_sync(lambda db: apply_received_fill(
            db, fill_id=existing.id, user_id=user_id, settlement=settlement))
        await _sync_order_sheet_fill_telemetry(session, user_id=user_id, proposal_id=proposal_id)
        await session.commit()
        return ManualFillResult(
            fill_id=existing.id,
            created=False,
            proposal_id=proposal.id,
            proposal_status=proposal.status,
            pending_order_id=pending.id,
            pending_status=pending.status,
            broker=broker,
            account_id=proposal.account_id,
            filled_quantity=float(total),
            target_quantity=float(proposal.size_shares_or_currency),
            book_status=application.status, book_reason=application.reason,
            applied_snapshot_id=application.applied_snapshot_id,
        )

    target = ledger_amount(proposal.size_shares_or_currency)
    already_filled = await _filled_quantity(session, user_id, proposal_id)
    resulting_quantity = already_filled + quantity_exact
    if resulting_quantity > target:
        raise ValueError(
            f"cumulative fill quantity {resulting_quantity:g} exceeds approved "
            f"quantity {target:g}"
        )

    fill = BrokerFill(
        proposal_id=proposal.id,
        broker=broker,
        broker_order_id=order_id,
        external_fill_id=execution_id,
        account_id=proposal.account_id,
        ticker=proposal.ticker,
        action=proposal.action,
        quantity=float(quantity_exact),
        price=float(price_exact),
        commission=float(commission_exact) if commission_exact is not None else 0.0,
        commission_confirmed=commission_exact is not None,
        price_currency=settlement.currency if settlement else None,
        commission_currency=settlement.currency if settlement else None,
        filled_at=filled_at or datetime.now(UTC),
        paper=False,
        settlement=settlement,
        execution_time_confirmed=filled_at is not None,
    )
    inserted = await persist_broker_fill(
        session,
        user_id=user_id,
        proposal_id=proposal.id,
        account_id=proposal.account_id,
        fill=fill,
        apply_to_book=False,
    )
    if not inserted:  # pragma: no cover - protects a concurrent duplicate
        raise RuntimeError("fill was concurrently recorded; retry the request")
    # Validate the newly persisted receipt too before publishing completion.
    resulting_quantity = await _filled_quantity(session, user_id, proposal_id)

    fill_row = (
        await session.execute(
            select(Fill).where(
                Fill.user_id == user_id,
                Fill.broker == broker,
                Fill.external_fill_id == execution_id,
            )
        )
    ).scalar_one()
    pending = (
        await session.execute(
            select(PendingOrder).where(
                PendingOrder.user_id == user_id,
                PendingOrder.proposal_id == proposal.id,
            )
        )
    ).scalar_one_or_none()
    if pending is not None and (
        pending.broker != broker or pending.broker_order_id != order_id or pending.account_id != proposal.account_id
    ):
        raise ValueError(
            "proposal already has a different broker order receipt"
        )
    complete = resulting_quantity == target
    pending_status = "filled" if complete else "partial"
    moment = datetime.now(UTC)
    if pending is None:
        pending = PendingOrder(
            user_id=user_id,
            proposal_id=proposal.id,
            broker=broker,
            broker_order_id=order_id,
            account_id=proposal.account_id,
            status=pending_status,
            last_polled_at=moment,
        )
        session.add(pending)
        await session.flush()
    else:
        pending.status = pending_status
        pending.last_polled_at = moment

    if status is ProposalStatus.APPROVED:
        assert_legal(status, ProposalStatus.EXECUTED_LIVE)
        proposal.status = ProposalStatus.EXECUTED_LIVE.value
        proposal.updated_at = moment
        session.add(
            ProposalHistory(
                proposal_id=proposal.id,
                status=proposal.status,
                transitioned_at=moment,
                transitioned_by="manual_fill_capture",
                note=f"Broker-confirmed execution at {broker}",
            )
        )
        await record_audit_event(
            user_id=user_id,
            event_type="proposal.transition",
            entity_type="proposal",
            entity_id=str(proposal.id),
            payload={
                "src": status.value,
                "dst": proposal.status,
                "actor": "manual_fill_capture",
                "note": f"Broker-confirmed execution at {broker}",
            },
            session=session,
        )

    await _resolve_manual_adapter_barrier(session, pending)
    from argosy.execution.fill_book import apply_received_fill

    application = await session.run_sync(lambda db: apply_received_fill(
        db, fill_id=fill_row.id, user_id=user_id))
    await session.commit()
    return ManualFillResult(
        fill_id=fill_row.id,
        created=True,
        proposal_id=proposal.id,
        proposal_status=proposal.status,
        pending_order_id=pending.id,
        pending_status=pending.status,
        broker=broker,
        account_id=proposal.account_id,
        filled_quantity=float(resulting_quantity),
        target_quantity=float(target),
        book_status=application.status, book_reason=application.reason,
        applied_snapshot_id=application.applied_snapshot_id,
    )


async def _filled_quantity(session, user_id: str, proposal_id: int) -> Decimal:
    rows = (
        await session.execute(
            select(Fill).where(
                Fill.proposal_id == proposal_id,
            )
        )
    ).scalars().all()
    proposal = await session.get(Proposal, proposal_id)
    if proposal is None or proposal.user_id != user_id:
        raise ValueError("Proposal ownership is unproven")
    evidence = reconcile_fill_evidence(proposal, list(rows))
    if evidence.errors:
        raise ValueError("Existing receipts need reconciliation: " + "; ".join(evidence.errors))
    return evidence.quantity


async def _resolve_manual_adapter_barrier(session, pending: PendingOrder) -> None:
    # Called only after the manual path verifies exact order identity and all
    # saved execution evidence. Never clears a data/identity staging failure.
    if pending.broker in MANUAL_RECEIPT_BROKERS and pending.receipt_sync_error == "broker adapter unavailable":
        pending.receipt_sync_error = None
        await record_audit_event(user_id=pending.user_id, event_type="reconcile.manual_adapter_resolved",
            entity_type="pending_order", entity_id=str(pending.id),
            payload={"reason": "Verified manual-only receipt path; no remote status API"}, session=session)


__all__ = ["ManualFillResult", "record_manual_fill"]
