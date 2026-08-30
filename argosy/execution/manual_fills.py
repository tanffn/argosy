"""Trusted receipt path for orders placed at read-only brokers.

Schwab and Leumi adapters intentionally cannot place orders.  After the user
places an approved Argosy proposal in the broker UI, this service records the
broker's execution facts without letting request data redefine the approved
ticker, side, account, or target quantity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isclose, isfinite

from sqlalchemy import select

from argosy.adapters.brokers.types import Fill as BrokerFill
from argosy.decisions.proposals import ProposalStatus, assert_legal
from argosy.execution.audit import record_audit_event
from argosy.execution.reconcile import persist_broker_fill
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
    commission: float = 0.0,
    filled_at: datetime | None = None,
) -> ManualFillResult:
    """Record one broker-confirmed execution against an exact proposal.

    Partial fills are supported.  The first receipt moves the proposal from
    ``approved`` to ``executed_live``; its pending-order receipt stays
    ``partial`` until cumulative fill quantity reaches the approved quantity.
    Replaying the same broker execution id is idempotent.
    """
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
    if not isfinite(quantity) or quantity <= 0:
        raise ValueError("quantity must be positive")
    if not isfinite(price) or price <= 0:
        raise ValueError("price must be positive")
    if not isfinite(commission) or commission < 0:
        raise ValueError("commission cannot be negative")

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
        if (
            existing.proposal_id != proposal_id
            or existing.broker_order_id != order_id
            or not isclose(float(existing.quantity), quantity, abs_tol=0.0001)
            or not isclose(float(existing.price), price, abs_tol=0.0001)
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
        total = await _filled_quantity(session, user_id, proposal_id)
        return ManualFillResult(
            fill_id=existing.id,
            created=False,
            proposal_id=proposal.id,
            proposal_status=proposal.status,
            pending_order_id=pending.id,
            pending_status=pending.status,
            broker=broker,
            account_id=proposal.account_id,
            filled_quantity=total,
            target_quantity=float(proposal.size_shares_or_currency),
        )

    target = float(proposal.size_shares_or_currency)
    already_filled = await _filled_quantity(session, user_id, proposal_id)
    resulting_quantity = already_filled + quantity
    if resulting_quantity > target and not isclose(
        resulting_quantity, target, abs_tol=0.0001
    ):
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
        quantity=quantity,
        price=price,
        commission=commission,
        filled_at=filled_at or datetime.now(UTC),
        paper=False,
    )
    inserted = await persist_broker_fill(
        session,
        user_id=user_id,
        proposal_id=proposal.id,
        account_id=proposal.account_id,
        fill=fill,
    )
    if not inserted:  # pragma: no cover - protects a concurrent duplicate
        raise RuntimeError("fill was concurrently recorded; retry the request")

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
        pending.broker != broker or pending.broker_order_id != order_id
    ):
        raise ValueError(
            "proposal already has a different broker order receipt"
        )
    complete = resulting_quantity >= target or isclose(
        resulting_quantity, target, abs_tol=0.0001
    )
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
        filled_quantity=resulting_quantity,
        target_quantity=target,
    )


async def _filled_quantity(session, user_id: str, proposal_id: int) -> float:
    rows = (
        await session.execute(
            select(Fill.quantity).where(
                Fill.user_id == user_id,
                Fill.proposal_id == proposal_id,
                Fill.paper.is_(False),
            )
        )
    ).scalars().all()
    return sum(float(value) for value in rows)


__all__ = ["ManualFillResult", "record_manual_fill"]
