"""Risk officer + facilitator tests; mock Anthropic."""

from __future__ import annotations

import json
from typing import Any

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.risk_facilitator import RiskFacilitatorAgent, RiskOutcome
from argosy.agents.risk_officer import RiskOfficerAgent, RiskVerdict


def _mock(cls, canned: dict, **init_kwargs):
    class _M(cls):  # type: ignore[misc, valid-type]
        async def _call_model(self, *, system: str, user: str, **_extra: Any) -> ModelCall:
            return ModelCall(
                text=json.dumps(canned),
                tokens_in=80,
                tokens_out=120,
                model=self.model,
            )
    return _M


def _verdict(perspective: str, verdict: str = "APPROVE", round_index: int = 1) -> dict:
    return {
        "perspective": perspective,
        "round_index": round_index,
        "verdict": verdict,
        "conditions": ["cut size 50%"] if verdict == "APPROVE_WITH_CONDITIONS" else [],
        "concerns": [
            {
                "concern": "Vol elevated",
                "evidence": "Tech RSI 78",
                "cited_sources": ["analyst:technical"],
            }
        ],
        "response_to_opposing": "",
        "confidence": "MEDIUM",
        "cited_sources": ["analyst:technical"],
    }


@pytest.mark.asyncio
async def test_risk_officer_aggressive_approve() -> None:
    canned = _verdict("aggressive", "APPROVE")
    agent = _mock(RiskOfficerAgent, canned)(user_id="ariel", perspective="aggressive")
    rep = await agent.run(
        proposal={"ticker": "AAPL", "action": "buy"},
        analyst_reports=[{"agent_role": "fundamentals", "cited_sources": ["x"]}],
        user_constraints="",
        risk_caps={},
        prior_rounds=None,
        round_index=1,
        n_max=1,
    )
    assert isinstance(rep.output, RiskVerdict)
    assert rep.output.perspective == "aggressive"
    assert rep.output.verdict == "APPROVE"


@pytest.mark.asyncio
async def test_risk_officer_conservative_reject() -> None:
    canned = _verdict("conservative", "REJECT")
    agent = _mock(RiskOfficerAgent, canned)(
        user_id="ariel", perspective="conservative"
    )
    rep = await agent.run(
        proposal={"ticker": "AAPL"},
        analyst_reports=[{"agent_role": "fundamentals", "cited_sources": ["x"]}],
        user_constraints="",
        risk_caps={},
    )
    assert rep.output.verdict == "REJECT"


def test_risk_officer_unknown_perspective_raises() -> None:
    with pytest.raises(ValueError):
        RiskOfficerAgent(user_id="ariel", perspective="bogus")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_facilitator_extracts_consensus_approve() -> None:
    canned = {
        "consensus_verdict": "APPROVE",
        "consolidated_conditions": [],
        "dissent_summary": "",
        "rounds_run": 1,
        "confidence": "MEDIUM",
        "cited_sources": ["analyst:technical"],
    }
    agent = _mock(RiskFacilitatorAgent, canned)(user_id="ariel")
    rep = await agent.run(
        verdicts=[
            _verdict("aggressive", "APPROVE"),
            _verdict("neutral", "APPROVE"),
            _verdict("conservative", "APPROVE_WITH_CONDITIONS"),
        ],
        rounds_run=1,
    )
    out: RiskOutcome = rep.output  # type: ignore[assignment]
    assert out.consensus_verdict == "APPROVE"


# ---------------------------------------------------------------------------
# Wave 1 follow-up — user_directive threading for risk_officer / risk_facilitator
# ---------------------------------------------------------------------------


_DIRECTIVE = (
    "AGREED: max NVDA concentration is 12%.\n"
    "DISAGREED: tax-loss harvest urgency — user counter is defer to Q4.\n"
    "DEFERRED: FX hedge sizing."
)


def test_risk_officer_build_prompt_includes_user_directive_when_provided() -> None:
    """Risk officer must surface user_directive so it doesn't REJECT a
    proposal solely on a risk the user has already accepted. Closes the
    D1 self-review finding for Phase 4 risk officers.
    """
    agent = RiskOfficerAgent(user_id="ariel", perspective="conservative")
    sys, usr = agent.build_prompt(
        proposal={"ticker": "NVDA", "action": "buy"},
        analyst_reports=[{"agent_role": "fundamentals", "cited_sources": ["x"]}],
        user_constraints="",
        risk_caps={},
        prior_rounds=None,
        round_index=1,
        n_max=1,
        user_directive=_DIRECTIVE,
    )
    assert "USER DIRECTIVE PRESENT" in sys
    assert "AGREED: max NVDA concentration is 12%." in usr
    assert "DISAGREED: tax-loss harvest urgency" in usr
    assert "DEFERRED: FX hedge sizing." in usr
    # Officer-specific instruction language must accompany the pointer.
    assert "AGREED" in sys
    assert "DISAGREED" in sys
    assert "DEFERRED" in sys


def test_risk_officer_build_prompt_omits_directive_section_when_empty() -> None:
    agent = RiskOfficerAgent(user_id="ariel", perspective="conservative")
    base = dict(
        proposal={"ticker": "NVDA"},
        analyst_reports=[{"agent_role": "fundamentals", "cited_sources": ["x"]}],
        user_constraints="",
        risk_caps={},
        prior_rounds=None,
        round_index=1,
        n_max=1,
    )
    sys_a, usr_a = agent.build_prompt(**base)
    sys_b, usr_b = agent.build_prompt(**base, user_directive="")
    assert sys_a == sys_b
    assert usr_a == usr_b
    assert "USER DIRECTIVE" not in sys_a
    assert "USER DIRECTIVE" not in usr_a


def test_risk_facilitator_build_prompt_includes_user_directive_when_provided() -> None:
    """Risk facilitator must surface user_directive so the consensus
    tally doesn't treat an officer's REJECT on a resolved item as a
    real REJECT vote.
    """
    agent = RiskFacilitatorAgent(user_id="ariel")
    sys, usr = agent.build_prompt(
        verdicts=[_verdict("aggressive"), _verdict("neutral"), _verdict("conservative")],
        rounds_run=1,
        user_directive=_DIRECTIVE,
    )
    assert "USER DIRECTIVE PRESENT" in sys
    assert "AGREED: max NVDA concentration is 12%." in usr
    assert "DISAGREED: tax-loss harvest urgency" in usr
    assert "DEFERRED: FX hedge sizing." in usr
    assert "AGREED" in sys
    assert "DISAGREED" in sys
    assert "DEFERRED" in sys


def test_risk_facilitator_build_prompt_omits_directive_section_when_empty() -> None:
    agent = RiskFacilitatorAgent(user_id="ariel")
    base = dict(
        verdicts=[_verdict("aggressive"), _verdict("neutral"), _verdict("conservative")],
        rounds_run=1,
    )
    sys_a, usr_a = agent.build_prompt(**base)
    sys_b, usr_b = agent.build_prompt(**base, user_directive="")
    assert sys_a == sys_b
    assert usr_a == usr_b
    assert "USER DIRECTIVE" not in sys_a
    assert "USER DIRECTIVE" not in usr_a


@pytest.mark.asyncio
async def test_facilitator_escalates_on_split() -> None:
    canned = {
        "consensus_verdict": "ESCALATE",
        "consolidated_conditions": [],
        "dissent_summary": "Conservative officer rejects on capital-preservation grounds.",
        "rounds_run": 1,
        "confidence": "MEDIUM",
        "cited_sources": ["analyst:technical"],
    }
    agent = _mock(RiskFacilitatorAgent, canned)(user_id="ariel")
    rep = await agent.run(
        verdicts=[
            _verdict("aggressive", "APPROVE"),
            _verdict("neutral", "REJECT"),
            _verdict("conservative", "REJECT"),
        ],
        rounds_run=1,
    )
    out: RiskOutcome = rep.output  # type: ignore[assignment]
    assert out.consensus_verdict == "ESCALATE"


def test_risk_officer_defaults_to_opus_not_sonnet() -> None:
    """H6: the hardcoded 'claude-sonnet-4-6' shadowed the role default. Risk
    officers must default to Opus (accuracy over cost; the risk gate must not
    silently run on Sonnet). A user with no agent_settings.yaml override resolves
    to the role default."""
    from argosy.agents.base import DEFAULT_MODEL_BY_ROLE

    agent = RiskOfficerAgent(user_id="_no_settings_user_", perspective="neutral")
    assert agent.model == DEFAULT_MODEL_BY_ROLE["risk_officer"]
    assert "sonnet" not in agent.model
