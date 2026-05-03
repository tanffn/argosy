"""WeeklyReviewLoop test — mocks plan-critique and verifies persistence."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from argosy.agents.base import ModelCall
from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.api import events
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.orchestrator.loops.weekly_review import (
    WeeklyReviewInputs,
    WeeklyReviewLoop,
)
from argosy.state import db as db_mod
from argosy.state.models import PlanCritique, PlanVersion, User


_CRITIQUE_CANNED = {
    "plan_label": "Test Plan",
    "snapshot_label": "weekly_review:test",
    "overall_summary": "Plan looks reasonable; one RED on FX assumption.",
    "confidence": "MEDIUM",
    "cited_sources": ["domain_knowledge/tax/israel/capital_gains.md"],
    "findings": [
        {
            "plan_item_ref": "FX 3.09",
            "severity": "RED",
            "topic": "FX",
            "summary": "Stale FX assumption.",
            "evidence": ["Plan says 3.09; current 2.94."],
            "cited_sources": ["domain_knowledge/_meta/sources.md"],
            "recommended_action": "Refresh FX in plan.",
        }
    ],
}


def _mock(canned: dict):
    class _M(PlanCritiqueAgent):
        async def _call_model(self, *, system: str, user: str) -> ModelCall:
            return ModelCall(
                text=json.dumps(canned),
                tokens_in=100,
                tokens_out=200,
                model=self.model,
            )
    return _M


def _gather(_user_id: str) -> WeeklyReviewInputs:
    return WeeklyReviewInputs(
        user_id="ariel",
        plan_label="Test Plan",
        plan_markdown="# Plan\n\nContent.\n",
        plan_version_id=1,
        snapshot_label="weekly_review:test",
        snapshot_summary="(test)",
    )


@pytest.mark.asyncio
async def test_weekly_review_persists_critique_and_emits_event(engine: None) -> None:
    events._reset_for_tests()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(
            PlanVersion(
                id=1,
                user_id="ariel",
                version_label="Test Plan",
                source_path="(test)",
                raw_markdown="# Plan\n\n",
            )
        )
        await session.commit()

    received: list[str] = []
    sub_ctx = events.subscribe()
    q = await sub_ctx.__aenter__()

    loop = WeeklyReviewLoop(
        schedule=LoopSchedule(cron="0 18 * * SUN"),
        user_id="ariel",
        plan_critique_factory=lambda: _mock(_CRITIQUE_CANNED)(user_id="ariel"),
        gather_inputs=_gather,
    )

    await loop.tick()

    while not q.empty():
        received.append(q.get_nowait())
    await sub_ctx.__aexit__(None, None, None)

    # PlanCritique row landed.
    async with db_mod.get_session() as session:
        rows = (
            await session.execute(select(PlanCritique))
        ).scalars().all()
        assert len(rows) == 1

    assert any("weekly_review.flagged" in m for m in received), received


@pytest.mark.asyncio
async def test_weekly_review_skips_when_plan_missing(engine: None) -> None:
    """No plan ingested → no error, no event."""
    events._reset_for_tests()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    def _empty(_uid: str) -> WeeklyReviewInputs:
        return WeeklyReviewInputs(
            user_id="ariel",
            plan_label="(none)",
            plan_markdown="",
            plan_version_id=None,
            snapshot_label="(none)",
            snapshot_summary="",
        )

    loop = WeeklyReviewLoop(
        schedule=LoopSchedule(cron="0 18 * * SUN"),
        user_id="ariel",
        plan_critique_factory=lambda: _mock(_CRITIQUE_CANNED)(user_id="ariel"),
        gather_inputs=_empty,
    )

    await loop.tick()
    # No exception is success here.
