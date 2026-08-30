from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import argosy.orchestrator.loops.audit as audit_mod
from argosy.agents.audit_agent import AuditAgent
from argosy.agents.base import ModelCall
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import AgentReport, User


class _OpenCostGuard:
    async def should_pause_non_routine(self, **kwargs) -> bool:
        return False


@pytest.mark.real_seam
@pytest.mark.asyncio
async def test_audit_real_agent_dispatch_persists_report(monkeypatch, engine) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    monkeypatch.setattr(audit_mod, "get_cost_guard", lambda *a, **k: _OpenCostGuard())
    payload = {
        "findings": [{
            "agent_role": "deployment_author",
            "pattern": "Repeated low-confidence sizing.",
            "evidence_run_ids": [41, 42],
            "proposed_prompt_tweak": "State the sizing premise explicitly.",
            "severity": "warning",
        }],
        "summary": "One repeated weakness.",
        "week_start": "2026-08-19",
        "week_end": "2026-08-26",
        "runs_reviewed": 2,
        "confidence": "HIGH",
        "cited_sources": ["agent_reports:41,42"],
    }

    async def fake_call(self, *, system, user, **kwargs):
        assert "id=41" in user and "systematic" in system.lower()
        return ModelCall(
            text=json.dumps(payload), tokens_in=10, tokens_out=10, model="test-model"
        )

    monkeypatch.setattr(AuditAgent, "_call_model", fake_call)
    now = datetime(2026, 8, 26, tzinfo=UTC)
    inputs = audit_mod.AuditInputs(
        user_id="ariel",
        window_start=now - timedelta(days=7),
        window_end=now,
        reports_json=[{
            "id": 41,
            "agent_role": "deployment_author",
            "model": "test-model",
            "confidence": "LOW",
            "tokens_in": 10,
            "tokens_out": 10,
            "cost_usd": 0,
        }],
    )
    loop = audit_mod.AuditLoop(
        schedule=LoopSchedule(cron="0 19 * * SUN"),
        user_id="ariel",
        gather_inputs=lambda user_id: inputs,
    )
    result = await loop.tick(now=lambda: now)

    async with db_mod.get_session() as session:
        roles = (await session.execute(select(AgentReport.agent_role))).scalars().all()
    assert result == {"status": "ok", "findings_count": 1}
    assert roles == ["audit"]
