"""T3 cooling-off next-day re-check tests (Phase 5, SDD §10.4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from argosy.agent_settings import AgentSettings, ExecutionBlock
from argosy.decisions.recheck import DeltaCheck, T3RecheckRunner
from argosy.decisions.risk_preflight import (
    PreflightInputs,
    PreflightReport,
    PreflightResult,
    PreflightStatus,
)
from argosy.state import db as db_mod
from argosy.state.models import (
    AuditLog,
    Proposal as ProposalRow,
    User,
)


def _passing_preflight(_inputs: PreflightInputs) -> PreflightReport:
    return PreflightReport(
        results=[
            PreflightResult(
                check="all", status=PreflightStatus.PASS, message="ok"
            )
        ]
    )


def _failing_preflight(_inputs: PreflightInputs) -> PreflightReport:
    return PreflightReport(
        results=[
            PreflightResult(
                check="cash", status=PreflightStatus.HARD_FAIL, message="no cash"
            )
        ]
    )


async def _seed_t3_cooling() -> int:
    async with db_mod.get_session() as session:
        if await session.get(User, "ariel") is None:
            session.add(User(id="ariel"))
        row = ProposalRow(
            user_id="ariel",
            ticker="AAPL",
            action="buy",
            size_shares_or_currency=10,
            order_type="limit",
            limit_price=100.0,
            tier="T3",
            account_class="main",
            status="cooling",
            cooling_off_until=datetime.now(timezone.utc) - timedelta(seconds=5),
            rationale_summary="t",
            expected_impact_json="{}",
            confidence="MEDIUM",
            time_in_force="DAY",
        )
        session.add(row)
        await session.commit()
        return int(row.id)


@pytest.mark.asyncio
async def test_recheck_passes_with_no_delta(engine: None) -> None:
    pid = await _seed_t3_cooling()
    runner = T3RecheckRunner(
        settings=AgentSettings(execution=ExecutionBlock(default_mode="paper")),
        delta_detector=lambda row, _r: DeltaCheck(material=False, summary="stable"),
        preflight_runner=_passing_preflight,
    )
    outcome = await runner.run(pid)
    assert outcome.decision == "passed"
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "awaiting_human"
        events = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "recheck.passed")
            )
        ).scalars().all()
        assert len(events) == 1


@pytest.mark.asyncio
async def test_recheck_pauses_on_material_change(engine: None) -> None:
    pid = await _seed_t3_cooling()
    runner = T3RecheckRunner(
        settings=AgentSettings(execution=ExecutionBlock(default_mode="paper")),
        delta_detector=lambda row, _r: DeltaCheck(
            material=True, summary="news flipped thesis"
        ),
        preflight_runner=_passing_preflight,
    )
    outcome = await runner.run(pid)
    assert outcome.decision == "paused"
    assert "news flipped thesis" in outcome.note
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "blocked"
        events = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "recheck.paused")
            )
        ).scalars().all()
        assert len(events) == 1


@pytest.mark.asyncio
async def test_recheck_blocks_on_failed_preflight(engine: None) -> None:
    pid = await _seed_t3_cooling()
    runner = T3RecheckRunner(
        settings=AgentSettings(execution=ExecutionBlock(default_mode="paper")),
        delta_detector=lambda row, _r: DeltaCheck(material=False),
        preflight_runner=_failing_preflight,
    )
    outcome = await runner.run(pid)
    assert outcome.decision == "preflight_failed"
    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, pid)
        assert row.status == "blocked"


@pytest.mark.asyncio
async def test_recheck_rejects_non_cooling_proposal(engine: None) -> None:
    """Only proposals in COOLING are eligible."""
    async with db_mod.get_session() as session:
        if await session.get(User, "ariel") is None:
            session.add(User(id="ariel"))
        row = ProposalRow(
            user_id="ariel",
            ticker="AAPL",
            action="buy",
            size_shares_or_currency=1.0,
            tier="T3",
            account_class="main",
            status="awaiting_human",
            rationale_summary="t",
            expected_impact_json="{}",
            confidence="MEDIUM",
            time_in_force="DAY",
        )
        session.add(row)
        await session.commit()
        pid = int(row.id)
    runner = T3RecheckRunner(
        delta_detector=lambda row, _r: DeltaCheck(material=False),
        preflight_runner=_passing_preflight,
    )
    from argosy.decisions.proposals import IllegalTransitionError

    with pytest.raises(IllegalTransitionError):
        await runner.run(pid)
