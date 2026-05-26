"""FundManagerAgent tests; mock Anthropic."""

from __future__ import annotations

import json
from typing import Any

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.fund_manager import (
    FundManagerAgent,
    FundManagerDecision,
    FundManagerPlanRevisionDecision,
)


def _mock(canned: dict):
    class _M(FundManagerAgent):
        async def _call_model(self, *, system: str, user: str, **_extra: Any) -> ModelCall:
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


# ---------------------------------------------------------------------------
# Plan-revision decision_kind — Wave 2 schema tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_revision_approved_uses_correct_schema() -> None:
    """plan_revision decision_kind must validate against
    FundManagerPlanRevisionDecision, not FundManagerDecision."""
    canned = {
        "approved": True,
        "reasons": ["Constraints honored.", "Horizons cohere."],
        "cited_sources": ["domain_kb/il_tax.md", "risk_facilitator"],
    }
    agent = _mock(canned)
    rep = await agent.run(
        decision_kind="plan_revision",
        draft_plan='{"long": {}, "medium": {}, "short": {}}',
        risk_verdict="APPROVE",
    )
    out = rep.output
    assert isinstance(out, FundManagerPlanRevisionDecision)
    assert out.approved is True
    assert len(out.reasons) == 2
    assert out.cited_sources


@pytest.mark.asyncio
async def test_plan_revision_rejected_uses_correct_schema() -> None:
    """plan_revision decision_kind with approved=False."""
    canned = {
        "approved": False,
        "reasons": ["Hard constraint violated: max_single_equity_pct exceeded."],
        "cited_sources": ["domain_kb/constraints.md"],
    }
    agent = _mock(canned)
    rep = await agent.run(
        decision_kind="plan_revision",
        draft_plan='{"long": {}}',
        risk_verdict="REJECT",
    )
    out = rep.output
    assert isinstance(out, FundManagerPlanRevisionDecision)
    assert out.approved is False
    assert "Hard constraint violated" in out.reasons[0]


@pytest.mark.asyncio
async def test_trade_proposal_still_uses_fund_manager_decision() -> None:
    """Regression: trade_proposal (default) must NOT use the plan-revision schema."""
    canned = {
        "decision": "green_light",
        "reason": "All clear.",
        "required_conditions": [],
        "post_execution_checks": [],
        "confidence": "MEDIUM",
        "cited_sources": ["risk_facilitator"],
    }
    agent = _mock(canned)
    rep = await agent.run(
        decision_kind="trade_proposal",
        proposal={"ticker": "AAPL", "action": "buy", "size_shares_or_currency": 10},
        risk_outcome={"consensus_verdict": "APPROVE"},
        plan_critique=None,
        user_constraints="",
        tier="T2",
    )
    out = rep.output
    assert isinstance(out, FundManagerDecision)
    assert out.decision == "green_light"


def test_build_prompt_includes_user_directive_when_provided() -> None:
    """When the orchestrator threads a non-empty user_directive into the
    FM's plan_revision prompt, the FM's system prompt MUST include the
    directive verbatim plus the per-stance instructions that tell it to
    respect AGREED / DISAGREED / DEFERRED resolutions from the user.

    Without this, the FM re-raises the same objections the user has
    already resolved — exactly the failure mode this fix targets.
    """
    agent = FundManagerAgent(user_id="ariel")
    directive = (
        "AGREED: max NVDA concentration is 12%.\n"
        "DISAGREED: tax-loss harvest urgency — user counter is defer to Q4.\n"
        "DEFERRED: FX hedge sizing."
    )
    sys, usr = agent.build_prompt(
        decision_kind="plan_revision",
        draft_plan='{"long": {}}',
        risk_verdict="APPROVE",
        user_directive=directive,
    )
    # Post-fix (post-f8faaca): system holds the POINTER + instructions
    # for the three stances; verbatim directive content lives in the
    # user prompt to dodge the bundled claude.exe SDK's empty-output
    # path observed on plan_synthesizer with large variable content
    # in system prompts (synthesis #27 + #28 both reproduced).
    assert "USER DIRECTIVE PRESENT" in sys
    assert "AGREED: max NVDA concentration is 12%." in usr
    assert "DISAGREED: tax-loss harvest urgency" in usr
    assert "DEFERRED: FX hedge sizing." in usr
    # Instruction language for the three stances must be in system prompt.
    assert "do NOT re-raise" in sys
    assert "evaluate freshly" in sys
    assert "NEW objections" in sys


def test_build_prompt_omits_directive_section_when_empty() -> None:
    """Empty user_directive (default) MUST produce a byte-identical
    system prompt to the no-kwarg call. Guards the happy path on the
    monthly synthesis cycle that doesn't carry user feedback.
    """
    agent = FundManagerAgent(user_id="ariel")
    base = dict(
        decision_kind="plan_revision",
        draft_plan='{"long": {}}',
        risk_verdict="APPROVE",
    )
    sys_a, usr_a = agent.build_prompt(**base)
    sys_b, usr_b = agent.build_prompt(**base, user_directive="")
    assert sys_a == sys_b, (
        "empty user_directive must produce a byte-identical system prompt "
        "to the no-kwarg call"
    )
    assert usr_a == usr_b
    assert "USER DIRECTIVE PRESENT" not in sys_a
    assert "USER DIRECTIVE" not in usr_a


@pytest.mark.asyncio
async def test_default_decision_kind_is_trade_proposal() -> None:
    """Omitting decision_kind defaults to trade_proposal schema."""
    canned = {
        "decision": "block",
        "reason": "No reason.",
        "required_conditions": [],
        "post_execution_checks": [],
        "confidence": "LOW",
        "cited_sources": ["plan_critique:RED"],
    }
    agent = _mock(canned)
    rep = await agent.run(
        proposal={"ticker": "TSLA", "action": "sell", "size_shares_or_currency": 5},
        risk_outcome={"consensus_verdict": "REJECT"},
        plan_critique=None,
        user_constraints="",
        tier="T1",
    )
    assert isinstance(rep.output, FundManagerDecision)
