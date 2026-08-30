"""ReconcileLoop tests: pending_orders → fills + status transitions."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from argosy.adapters.brokers.types import Fill as FillModel
from argosy.execution.reconcile import ReconcileLoop, _OrderSnapshot
from argosy.state import db as db_mod
from argosy.state.models import (
    AuditLog,
    PendingOrder,
    Prediction,
    User,
)
from argosy.state.models import (
    Fill as FillRow,
)
from argosy.state.models import (
    Proposal as ProposalRow,
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
            account_id="ibkr_main",
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
@pytest.mark.real_seam
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
async def test_reconcile_repeated_partial_fill_is_idempotent(engine: None) -> None:
    pid, _ = await _seed()
    partial_fill = FillModel(
        proposal_id=pid,
        broker="ibkr",
        broker_order_id="brkr-1",
        external_fill_id="exec-1",
        account_id="ibkr_main",
        ticker="AAPL",
        action="buy",
        quantity=4,
        price=180.0,
    )
    adapter = MockAdapter(_OrderSnapshot(status="partial", fills=[partial_fill]))
    loop = ReconcileLoop(adapter_factory=lambda b: adapter)
    first = await loop.tick()
    second = await loop.tick()

    assert first["fills_recorded"] == 1
    assert second["fills_deduped"] == 1
    async with db_mod.get_session() as session:
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].external_fill_id == "exec-1"
        assert fills[0].account_id == "ibkr_main"


@pytest.mark.asyncio
async def test_filled_without_execution_details_stays_pollable(engine: None) -> None:
    _, po_id = await _seed()
    loop = ReconcileLoop(
        adapter_factory=lambda b: MockAdapter(
            _OrderSnapshot(status="filled", fills=[], reason="details lagging")
        )
    )
    summary = await loop.tick()
    async with db_mod.get_session() as session:
        po = await session.get(PendingOrder, po_id)
        assert po.status == "submitted"
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert fills == []
    assert summary["errors"] == 1


@pytest.mark.asyncio
async def test_fill_vwap_reanchors_order_sheet_prediction(engine: None) -> None:
    pid, _ = await _seed()
    authored_at = datetime(2026, 8, 1, tzinfo=UTC)
    filled_at = datetime(2026, 8, 25, 14, 0, tzinfo=UTC)
    async with db_mod.get_session() as session:
        session.add(
            Prediction(
                user_id="ariel",
                source="signal_stream:order_sheet",
                source_ref=json.dumps({"proposal_id": pid}),
                ticker="AAPL",
                direction="long",
                entry_price=175.0,
                timeframe_days=90,
                event_at=authored_at,
                evaluation_due_at=authored_at + timedelta(days=90),
                evaluation_method="order_sheet_due_date_v1",
                archived=0,
                provenance_weights_applied=0,
            )
        )
        await session.commit()
    fill = FillModel(
        broker="ibkr",
        broker_order_id="brkr-1",
        external_fill_id="exec-vwap-1",
        account_id="ibkr_main",
        ticker="AAPL",
        action="buy",
        quantity=10,
        price=180.25,
        commission=1.0,
        filled_at=filled_at,
    )
    await ReconcileLoop(
        adapter_factory=lambda b: MockAdapter(
            _OrderSnapshot(status="filled", fills=[fill])
        )
    ).tick()
    async with db_mod.get_session() as session:
        prediction = (
            await session.execute(select(Prediction))
        ).scalars().one()
        assert float(prediction.entry_price) == pytest.approx(180.25)
        assert prediction.event_at == filled_at.replace(tzinfo=None)
        telemetry = json.loads(prediction.source_ref)["fill_telemetry"]
        assert telemetry["quantity"] == 10
        assert telemetry["external_fill_ids"] == ["exec-vwap-1"]


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
