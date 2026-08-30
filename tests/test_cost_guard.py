"""CostGuard tests — pause + override + routine exemption."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from argosy.agent_settings import AgentSettings, CostBlock
from argosy.orchestrator.cost_guard import ROUTINE_LOOPS, CostGuard, reset_cost_guard
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

    moment = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
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

    moment = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
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

    moment = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
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

    moment = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
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
async def test_cost_guard_developer_mode_persistently_bypasses_budget(engine: None) -> None:
    reset_cost_guard()
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(
            AgentReport(
                user_id="ariel",
                agent_role="news",
                response_text="ok",
                cost_usd=500.0,
                model="claude-sonnet-4-6",
            )
        )
        await session.commit()

    moment = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    settings = _settings(budget=10.0, pause_pct=100.0)
    settings.cost.developer_mode = True
    guard = CostGuard(
        user_id="ariel",
        settings=settings,
        clock=lambda: moment,
    )

    assert guard.developer_mode is True
    assert await guard.should_pause_non_routine(loop_name="discovery_funnel") is False


@pytest.mark.asyncio
async def test_cost_guard_pauses_actual_loops(engine: None) -> None:
    """When CostGuard reports paused, the hour loop and minute loop skip
    their work (no audit row written, no events emitted).
    """
    from sqlalchemy import select

    from argosy.agent_settings import AgentSettings, CostBlock
    from argosy.api import events
    from argosy.orchestrator.cost_guard import get_cost_guard
    from argosy.orchestrator.loops.base import LoopSchedule
    from argosy.orchestrator.loops.hour_loop import HourLoop
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

    moment = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    g = CostGuard(
        user_id="ariel",
        settings=_settings(budget=0.0, pause_pct=100.0),
        clock=lambda: moment,
    )
    assert await g.should_pause_non_routine(loop_name="hour") is False


@pytest.mark.asyncio
async def test_watchlist_awaits_cost_guard_and_runs_when_not_paused(monkeypatch) -> None:
    from types import SimpleNamespace

    from argosy.orchestrator.loops import watchlist as watchlist_mod
    from argosy.orchestrator.loops.base import LoopSchedule

    checked = []
    gathered = []

    class Guard:
        async def should_pause_non_routine(self, *, loop_name=None):
            checked.append(loop_name)
            return False

    class Agent:
        async def run(self, **kwargs):
            return SimpleNamespace(output=SimpleNamespace(
                current_tickers=[], added_today=[], removed_today=[],
            ))

    async def gather(user_id):
        gathered.append(user_id)
        return watchlist_mod.WatchlistInputs(user_id, [], [], [], [], "positions")

    monkeypatch.setattr(watchlist_mod, "get_cost_guard", lambda: Guard())
    monkeypatch.setattr(watchlist_mod, "publish_event", lambda *a, **k: _noop())
    loop = watchlist_mod.WatchlistLoop(
        schedule=LoopSchedule(interval_seconds=3600),
        gather_inputs=gather,
        watchlist_agent_factory=Agent,
    )
    out = await loop.tick()

    assert checked == ["watchlist"]
    assert gathered == ["ariel"]
    assert out == {"status": "ok", "count": 0, "added": 0, "removed": 0}


@pytest.mark.asyncio
async def test_audit_awaits_cost_guard_and_reaches_gather_when_not_paused(
    monkeypatch,
) -> None:
    from argosy.orchestrator.loops import audit as audit_mod
    from argosy.orchestrator.loops.base import LoopSchedule

    checked = []
    gathered = []

    class Guard:
        async def should_pause_non_routine(self, *, loop_name=None):
            checked.append(loop_name)
            return False

    async def gather(user_id):
        gathered.append(user_id)
        now = datetime.now(UTC)
        return audit_mod.AuditInputs(user_id, now, now, [])

    monkeypatch.setattr(audit_mod, "get_cost_guard", lambda: Guard())
    loop = audit_mod.AuditLoop(
        schedule=LoopSchedule(interval_seconds=3600),
        gather_inputs=gather,
    )
    out = await loop.tick()

    assert checked == ["audit"]
    assert gathered == ["ariel"]
    assert out == {"status": "skipped", "reason": "no_reports"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "class_name", "expected_loop_name"),
    [
        (
            "argosy.orchestrator.loops.signal_streams_daily",
            "SignalStreamsDailyLoop",
            "signal_streams_daily",
        ),
        (
            "argosy.orchestrator.loops.discovery_funnel_loop",
            "DiscoveryFunnelLoop",
            "discovery_funnel",
        ),
        (
            "argosy.orchestrator.loops.decision_funnel_loop",
            "DecisionFunnelLoop",
            "decision_funnel",
        ),
        (
            "argosy.services.jobs.holdings_review",
            "HoldingsReviewJob",
            "holdings_review",
        ),
        (
            "argosy.services.jobs.period_directive_daily",
            "PeriodDirectiveDailyJob",
            "period_directive_daily",
        ),
    ],
)
async def test_agentic_discovery_paths_await_cost_guard_before_work(
    monkeypatch, module_name, class_name, expected_loop_name
) -> None:
    import importlib

    module = importlib.import_module(module_name)
    checked: list[str | None] = []

    class Guard:
        async def should_pause_non_routine(self, *, loop_name=None):
            checked.append(loop_name)
            return True

    monkeypatch.setattr(
        module,
        "get_cost_guard",
        lambda **_kwargs: Guard(),
    )
    loop = getattr(module, class_name)(user_id="ariel")

    result = await loop.tick()

    assert result == {"status": "paused", "reason": "cost_cap"}
    assert checked == [expected_loop_name]


async def _noop() -> None:
    return None
