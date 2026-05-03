"""ReconcileLoop tests: pending_orders → fills + status transitions."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from argosy.adapters.brokers.types import Fill as FillModel
from argosy.execution.reconcile import ReconcileLoop, _OrderSnapshot
from argosy.state import db as db_mod
from argosy.state.models import (
    AuditLog,
    Fill as FillRow,
    PendingOrder,
    Proposal as ProposalRow,
    User,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


async def _seed(*, user_id: str = "ariel") -> tuple[int, int]:
    """Create user, proposal (executed_live), pending_order. Return ids."""
    async with db_mod.get_session() as session:
        existing = await session.get(User, user_id)
        if existing is None:
            session.add(User(id=user_id))
            await session.flush()
        proposal = ProposalRow(
            user_id=user_id,
            ticker="AAPL",
            action="buy",
            size_shares_or_currency=10,
            tier="T1",
            account_class="main",
            status="executed_live",
            rationale_summary="r",
            expected_impact_json="{}",
            confidence="MEDIUM",
        )
        session.add(proposal)
        await session.flush()
        pending = PendingOrder(
            user_id=user_id,
            proposal_id=proposal.id,
            broker="ibkr",
            broker_order_id="brkr-1",
            status="submitted",
        )
        session.add(pending)
        await session.commit()
        return proposal.id, pending.id


class MockAdapter:
    """Adapter that exposes `get_order_snapshot` with canned outcomes."""

    def __init__(self, snapshot: _OrderSnapshot | None) -> None:
        self.snapshot = snapshot

    def get_open_orders(self, account_id):
        return []

    async def get_order_snapshot(self, broker_order_id: str):
        return self.snapshot


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_filled_writes_fill_row(engine: None) -> None:
    pid, _ = await _seed()
    fill = FillModel(
        proposal_id=pid,
        broker="ibkr",
        broker_order_id="brkr-1",
        ticker="AAPL",
        action="buy",
        quantity=10,
        price=180.0,
        commission=1.0,
    )
    snap = _OrderSnapshot(status="filled", fills=[fill])
    adapter = MockAdapter(snap)
    loop = ReconcileLoop(adapter_factory=lambda b: adapter)
    await loop.tick()

    async with db_mod.get_session() as session:
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].quantity == 10
        assert fills[0].price == 180.0
        assert fills[0].paper is False
        po = (await session.execute(select(PendingOrder))).scalars().all()[0]
        assert po.status == "filled"
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "fill.received")
            )
        ).scalars().all()
        assert len(audit) == 1


@pytest.mark.asyncio
async def test_reconcile_partial_keeps_status_partial(engine: None) -> None:
    pid, _ = await _seed()
    partial_fill = FillModel(
        proposal_id=pid,
        broker="ibkr",
        broker_order_id="brkr-1",
        ticker="AAPL",
        action="buy",
        quantity=4,
        price=180.0,
    )
    snap = _OrderSnapshot(status="partial", fills=[partial_fill])
    adapter = MockAdapter(snap)
    loop = ReconcileLoop(adapter_factory=lambda b: adapter)
    await loop.tick()

    async with db_mod.get_session() as session:
        po = (await session.execute(select(PendingOrder))).scalars().all()[0]
        assert po.status == "partial"
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].quantity == 4


@pytest.mark.asyncio
async def test_reconcile_cancelled_records_audit(engine: None) -> None:
    pid, _ = await _seed()
    snap = _OrderSnapshot(status="cancelled", fills=[], reason="user requested")
    adapter = MockAdapter(snap)
    loop = ReconcileLoop(adapter_factory=lambda b: adapter)
    await loop.tick()

    async with db_mod.get_session() as session:
        po = (await session.execute(select(PendingOrder))).scalars().all()[0]
        assert po.status == "cancelled"
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "order.cancelled")
            )
        ).scalars().all()
        assert len(audit) == 1


@pytest.mark.asyncio
async def test_reconcile_rejected_records_audit(engine: None) -> None:
    pid, _ = await _seed()
    snap = _OrderSnapshot(status="rejected", fills=[], reason="margin")
    adapter = MockAdapter(snap)
    loop = ReconcileLoop(adapter_factory=lambda b: adapter)
    await loop.tick()

    async with db_mod.get_session() as session:
        po = (await session.execute(select(PendingOrder))).scalars().all()[0]
        assert po.status == "rejected"
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "order.rejected")
            )
        ).scalars().all()
        assert len(audit) == 1


@pytest.mark.asyncio
async def test_reconcile_skips_already_terminal_pending(engine: None) -> None:
    pid, po_id = await _seed()
    async with db_mod.get_session() as session:
        po = await session.get(PendingOrder, po_id)
        po.status = "filled"
        await session.commit()
    snap = _OrderSnapshot(status="rejected", fills=[])
    adapter = MockAdapter(snap)
    loop = ReconcileLoop(adapter_factory=lambda b: adapter)
    await loop.tick()
    async with db_mod.get_session() as session:
        po = await session.get(PendingOrder, po_id)
        assert po.status == "filled"  # unchanged
