"""ExecutionRouter end-to-end tests with a mock adapter."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from argosy.adapters.brokers.types import ExecutionResult, ProposedOrder
from argosy.execution.audit import write_paper_fill
from argosy.execution.router import ExecutionRouter
from argosy.state import db as db_mod
from argosy.state.models import (
    AuditLog,
    Fill as FillRow,
    PendingOrder,
    Proposal as ProposalRow,
    User,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


async def _seed_user_and_proposal(
    *,
    user_id: str = "ariel",
    tier: str = "T1",
    account_class: str = "main",
    status: str = "approved",
    action: str = "buy",
    qty: float = 10,
    limit_price: float | None = 100.0,
) -> int:
    async with db_mod.get_session() as session:
        existing = await session.get(User, user_id)
        if existing is None:
            session.add(User(id=user_id))
            await session.flush()
        row = ProposalRow(
            user_id=user_id,
            ticker="AAPL",
            action=action,
            size_shares_or_currency=qty,
            size_units="shares",
            instrument="stock",
            order_type="limit" if limit_price is not None else "market",
            limit_price=limit_price,
            tier=tier,
            account_class=account_class,
            status=status,
            rationale_summary="test proposal",
            expected_impact_json="{}",
            confidence="MEDIUM",
            time_in_force="DAY",
        )
        session.add(row)
        await session.commit()
        return int(row.id)


# ----------------------------------------------------------------------
# Mock adapter
# ----------------------------------------------------------------------


class MockBroker:
    name = "ibkr"

    def __init__(self, *, paper_result: bool = True, live_status: str = "submitted") -> None:
        self.paper_result = paper_result
        self.live_status = live_status
        self.placed: list[ProposedOrder] = []

    def get_positions(self, account_id):  # pragma: no cover - unused
        return []

    def get_lots(self, account_id, ticker):  # pragma: no cover - unused
        return []

    def get_open_orders(self, account_id):  # pragma: no cover - unused
        return []

    async def place_order(self, order: ProposedOrder, paper: bool = True) -> ExecutionResult:
        self.placed.append(order)
        if paper:
            await write_paper_fill(
                user_id=order.user_id,
                broker=self.name,
                ticker=order.ticker,
                action=order.action,
                quantity=order.quantity,
                price=order.limit_price or 0.0,
                proposal_id=order.proposal_id,
                broker_order_id=order.client_order_id,
            )
            return ExecutionResult(
                status="paper",
                broker=self.name,
                paper=True,
                broker_order_id=order.client_order_id,
            )
        return ExecutionResult(
            status=self.live_status,
            broker=self.name,
            broker_order_id="broker-123",
            paper=False,
        )

    async def cancel_order(self, order_id):  # pragma: no cover - unused
        from argosy.adapters.brokers.types import CancellationResult

        return CancellationResult(status="cancelled", broker=self.name, broker_order_id=order_id)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_paper_path_writes_paper_fill_and_advances(engine: None) -> None:
    pid = await _seed_user_and_proposal()
    mock = MockBroker()
    router = ExecutionRouter(
        user_id="ariel", adapter_factories={"ibkr": lambda: mock}
    )
    # Default agent_settings.execution.default_mode = paper
    result = await router.execute(pid, cash_available_usd=100_000.0)
    assert result.status == "paper"
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "executed_paper"
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].paper is True
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "preflight.completed")
            )
        ).scalars().all()
        assert len(audit) == 1
        transitions = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "proposal.transition")
            )
        ).scalars().all()
        assert any("executed_paper" in a.payload_json for a in transitions)


@pytest.mark.asyncio
async def test_router_live_path_creates_pending_and_advances(engine: None) -> None:
    from argosy.agent_settings import AgentSettings, ExecutionBlock

    pid = await _seed_user_and_proposal()
    mock = MockBroker(live_status="submitted")
    settings = AgentSettings(execution=ExecutionBlock(default_mode="live"))
    router = ExecutionRouter(
        user_id="ariel",
        settings=settings,
        adapter_factories={"ibkr": lambda: mock},
    )
    result = await router.execute(pid, cash_available_usd=100_000.0)
    assert result.status == "submitted"
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "executed_live"
        pending = (await session.execute(select(PendingOrder))).scalars().all()
        assert len(pending) == 1
        assert pending[0].broker_order_id == "broker-123"
        # No paper fill written
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 0


@pytest.mark.asyncio
async def test_router_preflight_hard_fail_cancels_proposal(engine: None) -> None:
    pid = await _seed_user_and_proposal(qty=10, limit_price=10_000.0)
    mock = MockBroker()
    router = ExecutionRouter(
        user_id="ariel", adapter_factories={"ibkr": lambda: mock}
    )
    # Estimated cost = 10 * 10000 = 100_000; available cash = 1.
    result = await router.execute(pid, cash_available_usd=1.0)
    assert result.status == "rejected"
    assert "BLOCKED" in result.reason or "cash" in result.reason.lower()
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "cancelled"
        # No order placed.
        assert mock.placed == []


@pytest.mark.asyncio
async def test_router_rejects_non_approved_proposal(engine: None) -> None:
    pid = await _seed_user_and_proposal(status="awaiting_human")
    mock = MockBroker()
    router = ExecutionRouter(
        user_id="ariel", adapter_factories={"ibkr": lambda: mock}
    )
    from argosy.decisions.proposals import IllegalTransitionError

    with pytest.raises(IllegalTransitionError):
        await router.execute(pid, cash_available_usd=100_000.0)


@pytest.mark.asyncio
async def test_router_unknown_proposal(engine: None) -> None:
    router = ExecutionRouter(user_id="ariel")
    with pytest.raises(LookupError):
        await router.execute(99999)
