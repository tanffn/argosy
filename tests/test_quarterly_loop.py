"""QuarterlyLoop tests."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from argosy.agents.base import ModelCall
from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.api import events
from argosy.orchestrator.cost_guard import reset_cost_guard
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.orchestrator.loops.quarterly import QuarterlyLoop
from argosy.state import db as db_mod
from argosy.state.models import AuditLog, PlanVersion, User


_CANNED = {
    "plan_label": "Test Plan",
    "snapshot_label": "quarterly:test",
    "overall_summary": "Plan-drift check: minor drift only.",
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
async def test_quarterly_emits_prompts_and_records_audit(engine: None) -> None:
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(
            PlanVersion(
                id=1,
                user_id="ariel",
                version_label="Test Plan",
                source_path="(test)",
                raw_markdown="# Plan\n",
            )
        )
        await session.commit()

    sub_ctx = events.subscribe()
    q = await sub_ctx.__aenter__()

    loop = QuarterlyLoop(
        schedule=LoopSchedule(cron="0 8 1 1,4,7,10 *"),
        user_id="ariel",
        plan_critique_factory=_mock_factory,
    )
    await loop.tick()

    received: list[str] = []
    while not q.empty():
        received.append(q.get_nowait())
    await sub_ctx.__aexit__(None, None, None)

    joined = "\n".join(received)
    assert "real_estate_pnl" in joined
    assert "bonus_event" in joined

    async with db_mod.get_session() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "quarterly.completed")
            )
        ).scalars().all()
    assert len(audits) == 1
