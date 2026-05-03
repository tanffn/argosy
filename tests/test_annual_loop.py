"""AnnualLoop tests."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from argosy.agents.base import ModelCall
from argosy.agents.domain_refresh import DomainRefreshAgent
from argosy.api import events
from argosy.orchestrator.cost_guard import reset_cost_guard
from argosy.orchestrator.loops.annual import AnnualLoop
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import AuditLog, User


_REFRESH_CANNED = {
    "per_file": [
        {
            "path": "domain_knowledge/tax/israel/capital_gains.md",
            "status": "no_change",
            "diff": None,
            "evidence": [
                {
                    "url": "https://taxes.gov.il/",
                    "retrieved_at": "2026-01-02",
                    "excerpt": "25%.",
                    "tier": 1,
                }
            ],
            "next_refresh_due": "2026-04-02",
            "note": "verified",
        }
    ],
    "summary": "1 file checked.",
    "confidence": "HIGH",
    "cited_sources": ["https://taxes.gov.il/"],
}


def _mock_refresh_factory():
    class _M(DomainRefreshAgent):
        async def _call_model(self, *, system: str, user: str) -> ModelCall:
            return ModelCall(
                text=json.dumps(_REFRESH_CANNED),
                tokens_in=200,
                tokens_out=300,
                model=self.model,
            )
    return _M(user_id="ariel")


@pytest.mark.asyncio
async def test_annual_emits_prompts_and_runs_refresh(engine: None) -> None:
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    sub_ctx = events.subscribe()
    q = await sub_ctx.__aenter__()

    loop = AnnualLoop(
        schedule=LoopSchedule(cron="0 8 2 1 *"),
        user_id="ariel",
        domain_refresh_factory=_mock_refresh_factory,
        domain_files_provider=lambda: [
            {
                "path": "domain_knowledge/tax/israel/capital_gains.md",
                "frontmatter": "next_refresh_due: 2026-04-01",
                "content": "Capital gains 25%.",
            }
        ],
    )
    await loop.tick()

    received: list[str] = []
    while not q.empty():
        received.append(q.get_nowait())
    await sub_ctx.__aexit__(None, None, None)

    joined = "\n".join(received)
    assert "tax_filing_prep" in joined
    assert "w8ben_refresh" in joined
    assert "insurance_renewal" in joined

    async with db_mod.get_session() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "annual.completed")
            )
        ).scalars().all()
    assert len(audits) == 1
    assert "files_reviewed" in audits[0].payload_json


@pytest.mark.asyncio
async def test_annual_with_no_files_still_records_audit(engine: None) -> None:
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    loop = AnnualLoop(
        schedule=LoopSchedule(cron="0 8 2 1 *"),
        user_id="ariel",
        domain_refresh_factory=_mock_refresh_factory,
        domain_files_provider=lambda: [],
    )
    await loop.tick()

    async with db_mod.get_session() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "annual.completed")
            )
        ).scalars().all()
    assert len(audits) == 1
