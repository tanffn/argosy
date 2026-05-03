"""FundManagerAgent tests; mock Anthropic."""

from __future__ import annotations

import json

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.fund_manager import FundManagerAgent, FundManagerDecision


def _mock(canned: dict):
    class _M(FundManagerAgent):
        async def _call_model(self, *, system: str, user: str) -> ModelCall:
            return ModelCall(
                text=json.dumps(canned),
                tokens_in=120,
                tokens_out=160,
                model=self.model,
            )
    return _M(user_id="ariel")


@pytest.mark.asyncio
async def test_fund_manager_green_lights() -> None:
    canned = {
        "decision": "green_light",
        "reason": "All risk approve, no plan RED, constraints aligned.",
        "required_conditions": [],
        "post_execution_checks": ["concentration < 65% post-fill"],
        "confidence": "HIGH",
        "cited_sources": ["risk_facilitator", "plan_critique:GREEN"],
    }
    agent = _mock(canned)
    assert agent.model == "claude-opus-4-7"
    rep = await agent.run(
        proposal={"ticker": "AAPL", "action": "buy", "size_shares_or_currency": 50},
        risk_outcome={"consensus_verdict": "APPROVE"},
        plan_critique={"findings": []},
        user_constraints="",
        tier="T2",
    )
    out: FundManagerDecision = rep.output  # type: ignore[assignment]
    assert out.decision == "green_light"
    assert out.cited_sources


@pytest.mark.asyncio
async def test_fund_manager_blocks_on_plan_red() -> None:
    canned = {
        "decision": "block",
        "reason": "Plan-critique RED on NVDA concentration; trade conflicts with reduction schedule.",
        "required_conditions": [],
        "post_execution_checks": [],
        "confidence": "HIGH",
        "cited_sources": ["plan_critique:RED"],
    }
    agent = _mock(canned)
    rep = await agent.run(
        proposal={"ticker": "NVDA", "action": "buy"},
        risk_outcome={"consensus_verdict": "APPROVE"},
        plan_critique={"findings": [{"severity": "RED", "topic": "Concentration"}]},
        user_constraints="",
        tier="T3",
    )
    assert rep.output.decision == "block"
