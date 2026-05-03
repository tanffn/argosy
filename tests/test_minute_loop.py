"""MinuteLoop tests — fake clock, deterministic price providers."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from argosy.orchestrator.cost_guard import reset_cost_guard
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.orchestrator.loops.minute_loop import MinuteLoop
from argosy.state import db as db_mod
from argosy.state.models import AuditLog, User


@pytest.mark.asyncio
async def test_minute_loop_records_target_breach(engine: None) -> None:
    reset_cost_guard()
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    watchlist = [
        {"ticker": "NVDA", "target_price": 150.0, "stop_price": 100.0},
    ]
    prices = {"NVDA": 200.0}

    loop = MinuteLoop(
        schedule=LoopSchedule(interval_seconds=60, market_hours_only=True),
        user_id="ariel",
        watchlist_provider=lambda: watchlist,
        price_provider=lambda t: prices.get(t),
    )
    fixed_now = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    await loop.tick(now=lambda: fixed_now)

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "minute_loop.trigger_recorded")
            )
        ).scalars().all()
    assert len(rows) == 1
    assert "target_breach" in rows[0].payload_json


@pytest.mark.asyncio
async def test_minute_loop_no_events_no_audit(engine: None) -> None:
    """When no thresholds breach, no audit row is written."""
    reset_cost_guard()
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    loop = MinuteLoop(
        schedule=LoopSchedule(interval_seconds=60, market_hours_only=True),
        user_id="ariel",
        watchlist_provider=lambda: [
            {"ticker": "NVDA", "target_price": 1000.0, "stop_price": 50.0}
        ],
        price_provider=lambda _: 200.0,
    )
    await loop.tick()

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "minute_loop.trigger_recorded")
            )
        ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_minute_loop_kill_switch_skips(engine: None) -> None:
    reset_cost_guard()
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    os.environ["ARGOSY_KILL"] = "1"
    try:
        loop = MinuteLoop(
            schedule=LoopSchedule(interval_seconds=60, market_hours_only=True),
            user_id="ariel",
            watchlist_provider=lambda: [
                {"ticker": "NVDA", "target_price": 100.0}
            ],
            price_provider=lambda _: 200.0,
        )
        await loop.tick()
    finally:
        del os.environ["ARGOSY_KILL"]

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "minute_loop.trigger_recorded")
            )
        ).scalars().all()
    assert rows == []
