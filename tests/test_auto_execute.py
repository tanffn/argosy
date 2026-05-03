"""Auto-execute path tests (Phase 5).

Limited+T0/T1 → auto-promote past awaiting_human → execute.
Limited+T2/T3 → never auto-promote.
queue_only mode disables every auto-execute cell regardless.
"""

from __future__ import annotations

import pytest

from argosy.adapters.brokers.types import ExecutionResult, ProposedOrder
from argosy.agent_settings import (
    AgentSettings,
    ExecutionBlock,
    LimitedAccountBlock,
)
from argosy.execution.audit import write_paper_fill
from argosy.execution.router import ExecutionRouter
from argosy.state import db as db_mod
from argosy.state.models import (
    AuditLog,
    Proposal as ProposalRow,
    User,
)


class _MockBroker:
    name = "ibkr"

    def __init__(self) -> None:
        self.placed: list[ProposedOrder] = []

    def get_positions(self, account_id):  # pragma: no cover
        return []

    def get_lots(self, account_id, ticker):  # pragma: no cover
        return []

    def get_open_orders(self, account_id):  # pragma: no cover
        return []

    async def place_order(self, order, paper: bool = True) -> ExecutionResult:
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
            status="submitted",
            broker=self.name,
            paper=False,
            broker_order_id="LIVE-1",
        )

    async def cancel_order(self, order_id):  # pragma: no cover
        from argosy.adapters.brokers.types import CancellationResult

        return CancellationResult(
            status="cancelled", broker=self.name, broker_order_id=order_id
        )


async def _seed_user(user_id: str = "ariel") -> None:
    async with db_mod.get_session() as session:
        if await session.get(User, user_id) is None:
            session.add(User(id=user_id))
            await session.commit()


async def _seed_proposal(
    *,
    tier: str,
    account_class: str,
    status: str = "awaiting_human",
    qty: float = 1.0,
    limit_price: float = 50.0,
) -> int:
    async with db_mod.get_session() as session:
        row = ProposalRow(
            user_id="ariel",
            ticker="AAPL",
            action="buy",
            size_shares_or_currency=qty,
            size_units="shares",
            instrument="stock",
            order_type="limit",
            limit_price=limit_price,
            tier=tier,
            account_class=account_class,
            status=status,
            rationale_summary="t",
            expected_impact_json="{}",
            confidence="MEDIUM",
            time_in_force="DAY",
        )
        session.add(row)
        await session.commit()
        return int(row.id)


def _settings(
    *,
    mode: str = "paper",
    limited_mode: str = "paper",
    size_usd: float = 1000.0,
) -> AgentSettings:
    return AgentSettings(
        execution=ExecutionBlock(default_mode=mode),  # type: ignore[arg-type]
        limited_account=LimitedAccountBlock(
            size_usd=size_usd,
            account_id="argonaut-1",
            execution_mode=limited_mode,  # type: ignore[arg-type]
            per_decision_max_pct=20.0,
        ),
    )


@pytest.mark.asyncio
async def test_limited_t0_auto_executes_paper(engine: None) -> None:
    await _seed_user()
    pid = await _seed_proposal(tier="T0", account_class="limited")
    broker = _MockBroker()
    router = ExecutionRouter(
        settings=_settings(),
        adapter_factories={"ibkr": broker},
    )
    result = await router.auto_execute_if_eligible(pid, cash_available_usd=10_000.0)
    assert result is not None, "expected ExecutionResult, got None"
    assert result.status == "paper", f"got {result.status}: {result.reason}"
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "executed_paper"
        # Audit trail records auto_promoted=True
        audit = (
            await session.execute(
                __import__("sqlalchemy").select(AuditLog).where(
                    AuditLog.event_type == "auto_execute.promoted"
                )
            )
        ).scalars().all()
        assert len(audit) == 1
        assert "auto_promoted" in audit[0].payload_json


@pytest.mark.asyncio
async def test_limited_t1_auto_executes_paper(engine: None) -> None:
    await _seed_user()
    pid = await _seed_proposal(tier="T1", account_class="limited")
    broker = _MockBroker()
    router = ExecutionRouter(
        settings=_settings(), adapter_factories={"ibkr": broker}
    )
    result = await router.auto_execute_if_eligible(pid, cash_available_usd=10_000.0)
    assert result is not None
    assert result.status == "paper", f"reason: {result.reason}"


@pytest.mark.asyncio
async def test_limited_t2_does_not_auto_execute(engine: None) -> None:
    await _seed_user()
    pid = await _seed_proposal(tier="T2", account_class="limited")
    router = ExecutionRouter(
        settings=_settings(), adapter_factories={"ibkr": _MockBroker()}
    )
    result = await router.auto_execute_if_eligible(pid)
    assert result is None
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "awaiting_human"


@pytest.mark.asyncio
async def test_limited_t3_does_not_auto_execute(engine: None) -> None:
    await _seed_user()
    pid = await _seed_proposal(tier="T3", account_class="limited")
    router = ExecutionRouter(
        settings=_settings(), adapter_factories={"ibkr": _MockBroker()}
    )
    result = await router.auto_execute_if_eligible(pid)
    assert result is None


@pytest.mark.asyncio
async def test_main_t0_does_not_auto_execute(engine: None) -> None:
    await _seed_user()
    pid = await _seed_proposal(tier="T0", account_class="main")
    router = ExecutionRouter(
        settings=_settings(), adapter_factories={"ibkr": _MockBroker()}
    )
    result = await router.auto_execute_if_eligible(pid)
    assert result is None


@pytest.mark.asyncio
async def test_queue_only_disables_auto_execute(engine: None) -> None:
    await _seed_user()
    pid = await _seed_proposal(tier="T0", account_class="limited")
    router = ExecutionRouter(
        settings=_settings(mode="queue_only"),
        adapter_factories={"ibkr": _MockBroker()},
    )
    result = await router.auto_execute_if_eligible(pid)
    assert result is None


@pytest.mark.asyncio
async def test_kill_switch_blocks_auto_execute(
    engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_user()
    pid = await _seed_proposal(tier="T0", account_class="limited")
    monkeypatch.setenv("ARGOSY_KILL", "1")
    router = ExecutionRouter(
        settings=_settings(), adapter_factories={"ibkr": _MockBroker()}
    )
    result = await router.auto_execute_if_eligible(pid)
    assert result is None
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        # Status unchanged
        assert row.status == "awaiting_human"
