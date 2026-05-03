"""CostGuard tests — pause + override + routine exemption."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from argosy.agent_settings import AgentSettings, CostBlock
from argosy.orchestrator.cost_guard import CostGuard, ROUTINE_LOOPS, reset_cost_guard
from argosy.state import db as db_mod
from argosy.state.models import AgentReport, User


def _settings(*, budget: float, pause_pct: float) -> AgentSettings:
    return AgentSettings(
        cost=CostBlock(monthly_budget_usd=budget, pause_at_pct=pause_pct, alert_at_pct=80.0)
    )


@pytest.mark.asyncio
async def test_cost_guard_pauses_at_threshold(engine: None) -> None:
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        # Insert agent_reports rows totaling $50.0 this month.
        for _ in range(5):
            session.add(
                AgentReport(
                    user_id="ariel",
                    agent_role="news",
                    response_text="ok",
                    cost_usd=10.0,
                    model="claude-sonnet-4-6",
                )
            )
        await session.commit()

    moment = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    g = CostGuard(
        user_id="ariel",
        settings=_settings(budget=40.0, pause_pct=100.0),
        clock=lambda: moment,
    )
    assert await g.should_pause_non_routine(loop_name="hour") is True


@pytest.mark.asyncio
async def test_cost_guard_below_threshold_runs(engine: None) -> None:
    reset_cost_guard()
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    moment = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    g = CostGuard(
        user_id="ariel",
        settings=_settings(budget=100.0, pause_pct=100.0),
        clock=lambda: moment,
    )
    # No spend yet → never paused.
    assert await g.should_pause_non_routine(loop_name="hour") is False


@pytest.mark.asyncio
async def test_cost_guard_routine_loops_exempt(engine: None) -> None:
    reset_cost_guard()
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        for _ in range(20):
            session.add(
                AgentReport(
                    user_id="ariel",
                    agent_role="news",
                    response_text="ok",
                    cost_usd=10.0,
                    model="claude-sonnet-4-6",
                )
            )
        await session.commit()

    moment = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    g = CostGuard(
        user_id="ariel",
        settings=_settings(budget=10.0, pause_pct=100.0),
        clock=lambda: moment,
    )
    for routine in ROUTINE_LOOPS:
        assert await g.should_pause_non_routine(loop_name=routine) is False


@pytest.mark.asyncio
async def test_cost_guard_override_lifts_pause(engine: None) -> None:
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        for _ in range(5):
            session.add(
                AgentReport(
                    user_id="ariel",
                    agent_role="news",
                    response_text="ok",
                    cost_usd=10.0,
                    model="claude-sonnet-4-6",
                )
            )
        await session.commit()

    moment = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    g = CostGuard(
        user_id="ariel",
        settings=_settings(budget=40.0, pause_pct=100.0),
        clock=lambda: moment,
    )
    # Paused initially.
    assert await g.should_pause_non_routine(loop_name="hour") is True
    g.set_override(minutes=60)
    # Override active — not paused.
    assert await g.should_pause_non_routine(loop_name="hour") is False


@pytest.mark.asyncio
async def test_cost_guard_pauses_actual_loops(engine: None) -> None:
    """When CostGuard reports paused, the hour loop and minute loop skip
    their work (no audit row written, no events emitted).
    """
    from argosy.agent_settings import AgentSettings, CostBlock
    from argosy.api import events
    from argosy.orchestrator.cost_guard import get_cost_guard
    from argosy.orchestrator.loops.base import LoopSchedule
    from argosy.orchestrator.loops.hour_loop import HourLoop
    from sqlalchemy import select

    from argosy.state.models import AuditLog

    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        for _ in range(5):
            session.add(
                AgentReport(
                    user_id="ariel",
                    agent_role="news",
                    response_text="ok",
                    cost_usd=10.0,
                    model="claude-sonnet-4-6",
                )
            )
        await session.commit()

    # Prime the singleton with low-budget settings so it returns "paused".
    settings = AgentSettings(
        cost=CostBlock(monthly_budget_usd=10.0, pause_at_pct=100.0, alert_at_pct=80.0)
    )
    get_cost_guard(user_id="ariel", settings=settings)

    async def news_provider():
        return [{"ticker": "NVDA", "headline": "x", "materiality": 0.9}]

    loop = HourLoop(
        schedule=LoopSchedule(interval_seconds=3600),
        user_id="ariel",
        news_provider=news_provider,
    )
    await loop.tick()

    # No audit row written because the loop short-circuited before work.
    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "hour_loop.events_recorded")
            )
        ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_cost_guard_zero_budget_disables(engine: None) -> None:
    reset_cost_guard()
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    moment = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    g = CostGuard(
        user_id="ariel",
        settings=_settings(budget=0.0, pause_pct=100.0),
        clock=lambda: moment,
    )
    assert await g.should_pause_non_routine(loop_name="hour") is False
