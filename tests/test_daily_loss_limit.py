"""Daily loss limit enforcement (Phase 5).

Per SDD §9.3, the daily loss limit is rule-based. Phase 5 wires the
limited-account variant: a configured pct of `size_usd` floors the
day's allowable P&L, and the execution-time preflight HARD_FAILs if
the *current* day P&L is already below the limit.
"""

from __future__ import annotations

from datetime import date as _date_cls

import pytest
from sqlalchemy import select

from argosy.adapters.brokers.types import ExecutionResult
from argosy.agent_settings import (
    AgentSettings,
    ExecutionBlock,
    LimitedAccountBlock,
)
from argosy.execution.router import ExecutionRouter
from argosy.state import db as db_mod
from argosy.state.models import (
    DailyAccountPnL,
    Proposal as ProposalRow,
    User,
)


class _MockBroker:
    name = "ibkr"

    async def place_order(self, order, paper: bool = True) -> ExecutionResult:
        return ExecutionResult(
            status="paper", broker=self.name, paper=True, broker_order_id="P"
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


async def _seed_user_and_proposal() -> int:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        row = ProposalRow(
            user_id="ariel",
            ticker="AAPL",
            action="buy",
            size_shares_or_currency=1.0,
            size_units="shares",
            order_type="limit",
            limit_price=50.0,
            tier="T0",
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


def _settings(*, size_usd: float = 1000.0, loss_pct: float = 5.0) -> AgentSettings:
    return AgentSettings(
        execution=ExecutionBlock(default_mode="paper"),
        limited_account=LimitedAccountBlock(
            size_usd=size_usd,
            account_id="argonaut-1",
            execution_mode="paper",
            per_decision_max_pct=20.0,
            daily_loss_limit_pct=loss_pct,
        ),
    )


@pytest.mark.asyncio
async def test_preflight_hard_fails_when_loss_breached(engine: None) -> None:
    pid = await _seed_user_and_proposal()
    today = _date_cls.today().isoformat()
    # Seed a daily P&L row that's already below the limit (-$60 vs -$50 floor).
    async with db_mod.get_session() as session:
        session.add(
            DailyAccountPnL(
                user_id="ariel",
                account_id="argonaut-1",
                date=today,
                realized_pnl_usd=-60.0,
                unrealized_pnl_usd=0.0,
            )
        )
        await session.commit()

    router = ExecutionRouter(
        settings=_settings(size_usd=1000.0, loss_pct=5.0),  # floor = -$50
        adapter_factories={"ibkr": _MockBroker()},
    )
    result = await router.execute(pid, cash_available_usd=10_000.0)
    assert result.status == "rejected"
    # The aggregator returns a summary "BLOCKED: N hard failure(s)..."; the
    # daily-loss-limit check IS one of those hard failures. Verify via the
    # audit log payload which captures per-check status.
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "cancelled"
        from argosy.state.models import AuditLog

        events = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "preflight.completed")
            )
        ).scalars().all()
        assert any(
            "daily_loss_limit" in e.payload_json and "HARD_FAIL" in e.payload_json
            for e in events
        )


@pytest.mark.asyncio
async def test_preflight_passes_when_within_loss_limit(engine: None) -> None:
    pid = await _seed_user_and_proposal()
    today = _date_cls.today().isoformat()
    # Seed a daily P&L only -$10 (within -$50 floor).
    async with db_mod.get_session() as session:
        session.add(
            DailyAccountPnL(
                user_id="ariel",
                account_id="argonaut-1",
                date=today,
                realized_pnl_usd=-10.0,
                unrealized_pnl_usd=0.0,
            )
        )
        await session.commit()
    router = ExecutionRouter(
        settings=_settings(size_usd=1000.0, loss_pct=5.0),
        adapter_factories={"ibkr": _MockBroker()},
    )
    result = await router.execute(pid, cash_available_usd=10_000.0)
    # Pass-through: the audit_log preflight payload should record the
    # daily_loss_limit check as PASS rather than HARD_FAIL.
    async with db_mod.get_session() as session:
        from argosy.state.models import AuditLog

        events = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "preflight.completed")
            )
        ).scalars().all()
        # When preflight ran, look for our check.
        if events:
            joined = "".join(e.payload_json for e in events)
            # daily_loss_limit must NOT be a hard failure here.
            assert (
                '"check": "daily_loss_limit", "status": "HARD_FAIL"' not in joined
            )


@pytest.mark.asyncio
async def test_get_daily_account_pnl_returns_zero_when_missing(engine: None) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()
    router = ExecutionRouter(settings=_settings())
    pnl = await router.get_daily_account_pnl_usd("argonaut-1")
    assert pnl == 0.0
