"""Round-trip the new Phase 1 SQLAlchemy models against in-memory SQLite."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport,
    AgentReportBlob,
    PlanCritique,
    PlanVersion,
    User,
    UserContext,
)


@pytest.mark.asyncio
async def test_plan_versions_and_critiques_roundtrip(engine: None) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(UserContext(user_id="ariel", current_stage="stage_1"))
        await session.flush()

        plan = PlanVersion(
            user_id="ariel",
            version_label="v2.0",
            source_path="/tmp/Jacobs_Wealth_Plan.md",
            raw_markdown="# Plan\n\nNVDA target 15%.\n",
        )
        session.add(plan)
        await session.flush()
        assert plan.id is not None

        critique = PlanCritique(
            user_id="ariel",
            plan_version_id=plan.id,
            critique_json='{"findings":[]}',
            model="claude-sonnet-4-6",
        )
        session.add(critique)
        await session.commit()

    async with db_mod.get_session() as session:
        rows = (await session.execute(select(PlanVersion))).scalars().all()
        assert len(rows) == 1
        assert rows[0].version_label == "v2.0"
        crit = (await session.execute(select(PlanCritique))).scalars().all()
        assert len(crit) == 1
        assert crit[0].plan_version_id == rows[0].id


@pytest.mark.asyncio
async def test_agent_reports_with_blobs(engine: None) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.flush()
        rpt = AgentReport(
            user_id="ariel",
            agent_role="intake",
            decision_id=None,
            prompt_hash="0" * 64,
            response_text="hello",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.0001,
            model="claude-sonnet-4-6",
            confidence="MEDIUM",
        )
        session.add(rpt)
        await session.flush()
        session.add(AgentReportBlob(report_id=rpt.id, key="output_json", value="{}"))
        await session.commit()

    async with db_mod.get_session() as session:
        rpt = (await session.execute(select(AgentReport))).scalars().one()
        assert rpt.agent_role == "intake"
        assert rpt.confidence == "MEDIUM"
        blob = (
            await session.execute(select(AgentReportBlob).where(AgentReportBlob.report_id == rpt.id))
        ).scalars().one()
        assert blob.key == "output_json"


@pytest.mark.asyncio
async def test_user_context_current_stage_nullable(engine: None) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="bob"))
        await session.flush()
        # current_stage is nullable; verify DB accepts NULL.
        session.add(UserContext(user_id="bob"))
        await session.commit()

    async with db_mod.get_session() as session:
        ctx = (await session.execute(select(UserContext).where(UserContext.user_id == "bob"))).scalar_one()
        assert ctx.current_stage is None
