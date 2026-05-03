"""MonthlyCycleLoop tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from argosy.agents.base import ModelCall
from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.orchestrator.cost_guard import reset_cost_guard
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.orchestrator.loops.monthly_cycle import MonthlyCycleLoop
from argosy.state import db as db_mod
from argosy.state.models import AuditLog, PlanCritique, PlanVersion, User


_CANNED = {
    "plan_label": "Test Plan",
    "snapshot_label": "monthly_cycle:test",
    "overall_summary": "Plan looks reasonable.",
    "confidence": "MEDIUM",
    "cited_sources": ["domain_knowledge/_meta/sources.md"],
    "findings": [],
}


def _mock_factory():
    class _M(PlanCritiqueAgent):
        async def _call_model(self, *, system: str, user: str) -> ModelCall:
            return ModelCall(
                text=json.dumps(_CANNED),
                tokens_in=100,
                tokens_out=200,
                model=self.model,
            )
    return _M(user_id="ariel")


@pytest.mark.asyncio
async def test_monthly_cycle_persists_critique_and_audit(engine: None) -> None:
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(
            PlanVersion(
                id=1,
                user_id="ariel",
                version_label="Test Plan",
                source_path="(test)",
                raw_markdown="# Plan\n\nContent.\n",
            )
        )
        await session.commit()

    async def fake_reconcile(_uid: str) -> dict[str, Any]:
        return {"status": "ok", "broker_imports": 0}

    async def fake_rsu(_uid: str) -> list[dict[str, Any]]:
        return []

    async def fake_buys(_uid: str) -> dict[str, Any]:
        return {"template": "flat", "items": []}

    loop = MonthlyCycleLoop(
        schedule=LoopSchedule(cron="0 8 1 * *"),
        user_id="ariel",
        plan_critique_factory=_mock_factory,
        statement_reconcile=fake_reconcile,
        rsu_vest_pull=fake_rsu,
        buy_template_generator=fake_buys,
    )
    await loop.tick()

    async with db_mod.get_session() as session:
        critiques = (await session.execute(select(PlanCritique))).scalars().all()
        audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "monthly_cycle.completed")
            )
        ).scalars().all()
    assert len(critiques) == 1
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_monthly_cycle_skips_critique_when_no_plan(engine: None) -> None:
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    loop = MonthlyCycleLoop(
        schedule=LoopSchedule(cron="0 8 1 * *"),
        user_id="ariel",
        plan_critique_factory=_mock_factory,
    )
    await loop.tick()

    async with db_mod.get_session() as session:
        critiques = (await session.execute(select(PlanCritique))).scalars().all()
        audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "monthly_cycle.completed")
            )
        ).scalars().all()
    assert critiques == []
    assert len(audits) == 1  # cycle still completed
