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
        async def _call_model(self, *, system: str, user: str, **_extra: Any) -> ModelCall:
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
async def test_monthly_cycle_skips_critique_when_no_plan(
    engine: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_cost_guard()
    # The default _real_rsu_pull now scans $ARGOSY_EXPENSE_SAMPLES_ROOT.
    # Unset it so this test doesn't walk the dev's Google Drive copy of
    # the Schwab CSV during a no-plan smoke run.
    monkeypatch.delenv("ARGOSY_EXPENSE_SAMPLES_ROOT", raising=False)

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


# ---------------------------------------------------------------------------
# Task 2.11: monthly_cycle fires plan_synthesis for every baseline user.
#
# These tests exercise the new sync module-level ``tick(session)`` entry
# point added in Task 2.11 (mirrors the plan_watcher pattern: a sync
# helper that the async ``MonthlyCycleLoop.tick`` bridges into via
# ``asyncio.to_thread``). The new entry point iterates every user with an
# active baseline plan and calls ``plan_synthesis.run_synthesis(...,
# trigger='scheduled')`` for each.
# ---------------------------------------------------------------------------


@pytest.fixture
def session_with_baseline(alembic_engine_at_head):
    from sqlalchemy.orm import sessionmaker
    from argosy.state.models import PlanVersion, User

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
    s.commit()
    yield s
    s.close()


def test_monthly_cycle_triggers_plan_synthesis(monkeypatch, session_with_baseline):
    """On the 1st of the month, monthly_cycle.tick must call run_synthesis."""
    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.loops import monthly_cycle

    calls = []

    def _fake_run(session, *, user_id, trigger, guidance=""):
        calls.append({"user_id": user_id, "trigger": trigger})
        class _R:
            decision_run_id = "test-run"
            draft_id = 999
        return _R()

    monkeypatch.setattr(flow, "run_synthesis", _fake_run)

    monthly_cycle.tick(session_with_baseline)

    user_ids = [c["user_id"] for c in calls]
    assert "ariel" in user_ids
    assert all(c["trigger"] == "scheduled" for c in calls)


def test_monthly_cycle_continues_after_one_user_fails(
    monkeypatch, alembic_engine_at_head
):
    """If run_synthesis raises for one user, the loop must continue for others."""
    from sqlalchemy.orm import sessionmaker

    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.loops import monthly_cycle
    from argosy.state.models import PlanVersion, User

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    try:
        s.add(User(id="ariel", plan="free"))
        s.add(User(id="bob", plan="free"))
        s.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
        s.add(PlanVersion(user_id="bob", role="baseline", raw_markdown="# Plan"))
        s.commit()

        seen: list[str] = []

        def _fake_run(session, *, user_id, trigger, guidance=""):
            seen.append(user_id)
            if user_id == "ariel":
                raise RuntimeError("boom")
            class _R:
                decision_run_id = "test-run"
                draft_id = 999
            return _R()

        monkeypatch.setattr(flow, "run_synthesis", _fake_run)

        # Must not raise — one user's failure must not stop the loop.
        monthly_cycle.tick(s)

        assert set(seen) == {"ariel", "bob"}
    finally:
        s.close()


def test_monthly_cycle_skips_users_with_no_baseline(
    monkeypatch, alembic_engine_at_head
):
    """Users without a role='baseline' row are simply not iterated."""
    from sqlalchemy.orm import sessionmaker

    from argosy.orchestrator.flows import plan_synthesis as flow
    from argosy.orchestrator.loops import monthly_cycle
    from argosy.state.models import User

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    try:
        s.add(User(id="newcomer", plan="free"))
        s.commit()

        calls: list[str] = []

        def _fake_run(session, *, user_id, trigger, guidance=""):
            calls.append(user_id)
            class _R:
                decision_run_id = "test-run"
                draft_id = 999
            return _R()

        monkeypatch.setattr(flow, "run_synthesis", _fake_run)

        monthly_cycle.tick(s)
        assert calls == []
    finally:
        s.close()
