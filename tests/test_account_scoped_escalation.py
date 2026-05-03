"""Account-scoped escalation re-check at execution time (Phase 5, SDD §4.3)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from argosy.adapters.brokers.types import ExecutionResult, ProposedOrder
from argosy.agent_settings import (
    AgentSettings,
    ExecutionBlock,
    LimitedAccountBlock,
)
from argosy.execution.router import ExecutionRouter
from argosy.state import db as db_mod
from argosy.state.models import (
    AuditLog,
    Proposal as ProposalRow,
    User,
)


class _MockBroker:
    name = "ibkr"

    async def place_order(self, order, paper: bool = True) -> ExecutionResult:
        return ExecutionResult(
            status="paper", broker=self.name, paper=True, broker_order_id="P1"
        )

    async def cancel_order(self, order_id):  # pragma: no cover
        from argosy.adapters.brokers.types import CancellationResult

        return CancellationResult(
            status="cancelled", broker=self.name, broker_order_id=order_id
        )

    def get_positions(self, account_id):  # pragma: no cover
        return []

    def get_lots(self, account_id, ticker):  # pragma: no cover
        return []

    def get_open_orders(self, account_id):  # pragma: no cover
        return []


async def _seed_user() -> None:
    async with db_mod.get_session() as session:
        if await session.get(User, "ariel") is None:
            session.add(User(id="ariel"))
            await session.commit()


async def _seed_approved_proposal(
    *,
    tier: str = "T1",
    qty: float = 5.0,
    limit_price: float = 100.0,
) -> int:
    """Approved limited-account proposal for $500 nominal."""
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
            account_class="limited",
            status="approved",
            rationale_summary="t",
            expected_impact_json="{}",
            confidence="MEDIUM",
            time_in_force="DAY",
        )
        session.add(row)
        await session.commit()
        return int(row.id)


def _settings(*, size_usd: float, threshold_pct: float = 20.0) -> AgentSettings:
    return AgentSettings(
        execution=ExecutionBlock(default_mode="paper"),
        limited_account=LimitedAccountBlock(
            size_usd=size_usd,
            account_id="argonaut-1",
            execution_mode="paper",
            per_decision_max_pct=threshold_pct,
        ),
    )


@pytest.mark.asyncio
async def test_escalation_blocks_when_account_shrunk(engine: None) -> None:
    """Trade is $500. Original account size was $5000 (10%, OK). Account
    has now shrunk to $1000 (50%, > 20% threshold) → block."""
    await _seed_user()
    pid = await _seed_approved_proposal(qty=5.0, limit_price=100.0)
    router = ExecutionRouter(
        settings=_settings(size_usd=1000.0),
        adapter_factories={"ibkr": _MockBroker()},
    )
    result = await router.execute(pid)
    assert result.status == "rejected"
    assert "Account-scoped escalation" in (result.reason or "")

    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "cancelled"
        events = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.event_type == "execution.account_escalation_block"
                )
            )
        ).scalars().all()
        assert len(events) == 1


@pytest.mark.asyncio
async def test_escalation_passes_when_account_still_large(engine: None) -> None:
    """Trade $500 against $5000 account = 10%, well under 20% threshold."""
    await _seed_user()
    pid = await _seed_approved_proposal(qty=5.0, limit_price=100.0)
    router = ExecutionRouter(
        settings=_settings(size_usd=5000.0),
        adapter_factories={"ibkr": _MockBroker()},
    )
    result = await router.execute(pid)
    # No escalation block: result.status reflects whatever broker did
    # (could be paper or rejected by another preflight on weekend; but
    # the reason MUST NOT be the escalation rejection).
    assert "Account-scoped escalation" not in (result.reason or "")


@pytest.mark.asyncio
async def test_escalation_skipped_for_main_account(engine: None) -> None:
    """The escalation rule applies only to the limited account."""
    await _seed_user()
    async with db_mod.get_session() as session:
        row = ProposalRow(
            user_id="ariel",
            ticker="AAPL",
            action="buy",
            size_shares_or_currency=10.0,
            limit_price=200.0,
            order_type="limit",
            tier="T1",
            account_class="main",
            status="approved",
            rationale_summary="t",
            expected_impact_json="{}",
            confidence="MEDIUM",
            time_in_force="DAY",
        )
        session.add(row)
        await session.commit()
        pid = int(row.id)
    router = ExecutionRouter(
        settings=_settings(size_usd=100.0),
        adapter_factories={"ibkr": _MockBroker()},
    )
    result = await router.execute(pid)
    assert "Account-scoped escalation" not in (result.reason or "")
