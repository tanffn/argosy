"""WeeklyReviewLoop test — mocks plan-critique and verifies persistence."""

from __future__ import annotations

import json
from typing import Any
from datetime import datetime, date

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
from argosy.state.models import PlanCritique, PlanVersion, User, UserContext, PortfolioSnapshotRow

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
        async def _call_model(self, *, system: str, user: str, **_extra: Any) -> ModelCall:
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
        user_context_yaml="staged_trims: true",
        domain_kb_files={"test/tax.md": "Tax evidence for test"},
    )


@pytest.mark.real_seam
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

    class EvidenceCheckingCritic(_mock(_CRITIQUE_CANNED)):
        def build_prompt(self, **kwargs):
            assert kwargs["snapshot_summary"] == "(test)"
            assert kwargs["user_context_yaml"] == "staged_trims: true"
            assert kwargs["domain_kb_files"] == {"test/tax.md": "Tax evidence for test"}
            return super().build_prompt(**kwargs)

    loop = WeeklyReviewLoop(
        schedule=LoopSchedule(cron="0 18 * * SUN"),
        user_id="ariel",
        plan_critique_factory=lambda: EvidenceCheckingCritic(user_id="ariel"),
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

@pytest.mark.asyncio
async def test_default_gather_renders_export_for_graph_authored_plan(engine: None) -> None:
    """Graph-authored plans have raw_markdown='' (living-plan cutover). The
    default gather must fall back to the canonical export render so the
    weekly critique reviews the REAL plan instead of no-op'ing with
    weekly_review.no_plan (live finding: plan_critiques was EMPTY)."""
    from argosy.orchestrator.loops.weekly_review import _default_gather_inputs

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(
            PlanVersion(
                id=1,
                user_id="ariel",
                version_label="x10-sleeve-draft",
                source_path="(graph)",
                raw_markdown="",  # graph-authored: no prose blob
            )
        )
        await session.commit()

    inputs = await _default_gather_inputs("ariel")
    assert inputs.plan_version_id == 1
    # The rendered export, not the empty raw_markdown — tick() would
    # otherwise return before calling the critique agent.
    assert inputs.plan_markdown.strip()
    assert "Argosy Plan Snapshot" in inputs.plan_markdown


@pytest.mark.asyncio
async def test_default_gather_prefers_authored_raw_markdown(engine: None) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(
            PlanVersion(
                id=1,
                user_id="ariel",
                version_label="baseline",
                source_path="(test)",
                raw_markdown="# My authored plan\n\nProse.\n",
            )
        )
        await session.commit()

    from argosy.orchestrator.loops.weekly_review import _default_gather_inputs

    inputs = await _default_gather_inputs("ariel")
    assert inputs.plan_markdown.strip() == "# My authored plan\n\nProse."


@pytest.mark.asyncio
async def test_default_gather_current_plan_and_scoped_dated_evidence(engine: None) -> None:
    from argosy.orchestrator.loops.weekly_review import _default_gather_inputs

    async with db_mod.get_session() as session:
        session.add_all([User(id="ariel"), User(id="other")])
        await session.flush()
        session.add_all([
            PlanVersion(id=1, user_id="ariel", role="current", raw_markdown="ACCEPTED",
                        imported_at=datetime(2026, 8, 24)),
            PlanVersion(id=2, user_id="ariel", role="draft", raw_markdown="REJECTED DRAFT",
                        imported_at=datetime(2026, 9, 1)),
            PlanVersion(id=3, user_id="other", role="current", raw_markdown="OTHER HOUSEHOLD"),
            UserContext(user_id="ariel", identity_yaml="citizenship: non-US",
                        goals_yaml="goal: growth", constraints_yaml="trim: staged"),
            PortfolioSnapshotRow(user_id="ariel", imported_at=datetime(2026, 8, 1),
                                 positions_json='[{"symbol":"OLDER"}]'),
            PortfolioSnapshotRow(user_id="ariel", imported_at=datetime(2026, 9, 30),
                                 snapshot_date=date(2026, 9, 11),
                                 positions_json='[{"symbol":"NVDA","currency":"USD"}]',
                                 totals_json='{"accounts_carried":["leumi"]}',
                                 parse_warnings_json='["unverified fund price"]'),
            PortfolioSnapshotRow(user_id="other", imported_at=datetime(2026, 10, 1),
                                 positions_json='[{"symbol":"OTHER"}]'),
        ])
        await session.commit()

    inputs = await _default_gather_inputs("ariel")
    assert inputs.plan_version_id == 1
    assert inputs.plan_markdown == "ACCEPTED"
    assert "2026-09-11" in inputs.snapshot_label
    assert '"NVDA"' in inputs.snapshot_summary
    assert '"OTHER"' not in inputs.snapshot_summary
    assert '"OLDER"' not in inputs.snapshot_summary
    assert "accounts_carried" in inputs.snapshot_summary
    assert "unverified fund price" in inputs.snapshot_summary
    assert "NOT live quotes" in inputs.snapshot_summary
    assert "citizenship: non-US" in inputs.user_context_yaml
    assert "trim: staged" in inputs.user_context_yaml
    assert "knowledge_review/current_status" in inputs.domain_kb_files
    other = await _default_gather_inputs("other")
    assert set(other.domain_kb_files) == {"knowledge_review/current_status"}


@pytest.mark.asyncio
async def test_graph_current_plan_content_cannot_switch_to_newer_draft(engine: None) -> None:
    from argosy.orchestrator.loops.weekly_review import _default_gather_inputs
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add_all([
            PlanVersion(id=1, user_id="ariel", role="current", version_label="ACCEPTED-GRAPH",
                        raw_markdown="", horizon_long_md="# Current thesis\nACCEPTED-HORIZON"),
            PlanVersion(id=2, user_id="ariel", role="draft", version_label="REJECTED-DRAFT",
                        raw_markdown="", horizon_long_md="# Rejected thesis\nDRAFT-HORIZON"),
        ])
        await session.commit()
    inputs = await _default_gather_inputs("ariel")
    assert inputs.plan_version_id == 1
    assert "ACCEPTED-GRAPH" in inputs.plan_markdown
    assert "REJECTED-DRAFT" not in inputs.plan_markdown
    assert "DRAFT-HORIZON" not in inputs.plan_markdown


@pytest.mark.asyncio
async def test_default_gather_draft_only_is_not_a_current_plan(engine: None) -> None:
    from argosy.orchestrator.loops.weekly_review import _default_gather_inputs
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(PlanVersion(user_id="ariel", role="draft", raw_markdown="NOT ACCEPTED"))
        await session.commit()
    inputs = await _default_gather_inputs("ariel")
    assert inputs.plan_version_id is None
    assert inputs.plan_markdown == ""
    assert "No saved portfolio snapshot" in inputs.snapshot_summary


@pytest.mark.asyncio
async def test_weekly_review_retries_wrapped_timeout_with_fresh_agent(engine, monkeypatch):
    from argosy.agents.errors import AgentRunError
    from argosy.services import fleet_reliability as reliability
    original = reliability.call_reliably_async
    async def immediate_retry(factory, **kwargs):
        return await original(factory, **kwargs, config=reliability.FleetRetryConfig(backoff_base_s=0),
                              breaker=reliability.CircuitBreaker())
    monkeypatch.setattr(reliability, "call_reliably_async", immediate_retry)
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(PlanVersion(id=1, user_id="ariel", version_label="test", source_path="test", raw_markdown="# Test"))
        await session.commit()
    agents = []
    class Critic(_mock(_CRITIQUE_CANNED)):
        async def run(self, **kwargs):
            if len(agents) == 1:
                try:
                    raise TimeoutError()
                except TimeoutError as cause:
                    raise AgentRunError("plan_critique: SDK error; stderr empty") from cause
            return await super().run(**kwargs)
    def factory():
        agent = Critic(user_id="ariel")
        agents.append(agent)
        return agent
    async def no_reconcile(**kwargs):
        pass
    loop = WeeklyReviewLoop(schedule=LoopSchedule(cron="0 18 * * SUN"), user_id="ariel",
                            plan_critique_factory=factory, gather_inputs=_gather, reconcile_fn=no_reconcile)
    await loop.tick()
    assert len(agents) == 2 and agents[0] is not agents[1]
    async with db_mod.get_session() as session:
        assert len((await session.scalars(select(PlanCritique))).all()) == 1
