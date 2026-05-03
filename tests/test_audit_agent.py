"""AuditAgent tests."""

from __future__ import annotations

import json

import pytest

from argosy.agents.audit_agent import AuditAgent, AuditReport, Finding
from argosy.agents.base import ModelCall


class _MockAuditAgent(AuditAgent):
    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output

    async def _call_model(self, *, system: str, user: str) -> ModelCall:
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=500,
            tokens_out=700,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_audit_report_shape() -> None:
    canned = {
        "findings": [
            {
                "agent_role": "sentiment",
                "pattern": "Sentiment analyst returned LOW confidence on 8/10 runs.",
                "evidence_run_ids": [101, 102, 103, 104, 105, 106, 107, 108],
                "proposed_prompt_tweak": "Lower the threshold for 'sufficient mentions'.",
                "severity": "warning",
            }
        ],
        "summary": "Sentiment data thin; one prompt tweak suggested.",
        "week_start": "2026-04-26",
        "week_end": "2026-05-02",
        "runs_reviewed": 47,
        "confidence": "MEDIUM",
        "cited_sources": ["agent_reports:101-108"],
    }
    agent = _MockAuditAgent(user_id="ariel", canned_output=canned)
    runs = [
        {
            "id": i,
            "agent_role": "sentiment",
            "model": "claude-haiku-4-5",
            "confidence": "LOW",
            "tokens_in": 100,
            "tokens_out": 200,
            "cost_usd": 0.001,
            "response_excerpt": "Insufficient mentions",
            "fund_manager_decision": "block",
        }
        for i in range(101, 109)
    ]
    report = await agent.run(
        runs_summary=runs,
        week_start="2026-04-26",
        week_end="2026-05-02",
    )
    out = report.output
    assert isinstance(out, AuditReport)
    assert isinstance(out.findings[0], Finding)
    assert out.findings[0].agent_role == "sentiment"
    assert out.cited_sources


@pytest.mark.asyncio
async def test_audit_empty_runs_handled() -> None:
    """Empty runs window produces an empty findings list."""
    canned = {
        "findings": [],
        "summary": "No runs in window.",
        "week_start": "2026-04-26",
        "week_end": "2026-05-02",
        "runs_reviewed": 0,
        "confidence": "HIGH",
        "cited_sources": ["agent_reports:none"],
    }
    agent = _MockAuditAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        runs_summary=[],
        week_start="2026-04-26",
        week_end="2026-05-02",
    )
    assert report.output.findings == []
