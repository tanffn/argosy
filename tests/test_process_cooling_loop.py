"""ProcessCoolingLoop tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from argosy.agent_settings import AgentSettings, ExecutionBlock
from argosy.api import events
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.orchestrator.loops.process_cooling import ProcessCoolingLoop
from argosy.state import db as db_mod
from argosy.state.models import (
    Proposal as ProposalRow,
    ProposalHistory,
    User,
)


async def _seed(
    *,
    cooling_offset_seconds: int = -60,
    account_class: str = "main",
    tier: str = "T3",
) -> int:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        row = ProposalRow(
            user_id="ariel",
            ticker="AAPL",
            action="buy",
            size_shares_or_currency=10,
            tier=tier,
            account_class=account_class,
            status="cooling",
            cooling_off_until=datetime.now(timezone.utc) + timedelta(seconds=cooling_offset_seconds),
            rationale_summary="test",
            expected_impact_json="{}",
            confidence="MEDIUM",
        )
        session.add(row)
        await session.commit()
        return row.id


@pytest.mark.asyncio
async def test_main_account_cooling_advances_to_awaiting_human(engine: None) -> None:
    events._reset_for_tests()
    pid = await _seed(account_class="main", tier="T3")

    settings = AgentSettings(execution=ExecutionBlock(default_mode="paper"))
    loop = ProcessCoolingLoop(
        schedule=LoopSchedule(interval_seconds=60),
        user_id="ariel",
        settings=settings,
    )
    await loop.tick()

    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "awaiting_human"
        history = (
            await session.execute(
                select(ProposalHistory).where(ProposalHistory.proposal_id == pid)
            )
        ).scalars().all()
        assert any(h.status == "awaiting_human" for h in history)


@pytest.mark.asyncio
async def test_limited_account_paper_short_circuit(engine: None) -> None:
    events._reset_for_tests()
    pid = await _seed(account_class="limited", tier="T2")

    settings = AgentSettings(execution=ExecutionBlock(default_mode="paper"))
    loop = ProcessCoolingLoop(
        schedule=LoopSchedule(interval_seconds=60),
        user_id="ariel",
        settings=settings,
    )
    await loop.tick()

    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "executed_paper"


@pytest.mark.asyncio
async def test_not_yet_ripe_proposals_are_left_alone(engine: None) -> None:
    """cooling_off_until in the future → no transition."""
    events._reset_for_tests()
    pid = await _seed(cooling_offset_seconds=3600, account_class="main", tier="T3")

    settings = AgentSettings(execution=ExecutionBlock(default_mode="paper"))
    loop = ProcessCoolingLoop(
        schedule=LoopSchedule(interval_seconds=60),
        user_id="ariel",
        settings=settings,
    )
    await loop.tick()

    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "cooling"
