"""Bull/bear researcher + facilitator tests; mock Anthropic."""

from __future__ import annotations

import json
from typing import Any

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.researcher import (
    BearResearcherAgent,
    BullResearcherAgent,
    ResearcherTurn,
)
from argosy.agents.researcher_facilitator import (
    DebateOutcome,
    ResearcherFacilitatorAgent,
)


def _mock(cls, canned: dict):
    class _M(cls):  # type: ignore[misc, valid-type]
        async def _call_model(self, *, system: str, user: str, **_extra: Any) -> ModelCall:
            return ModelCall(
                text=json.dumps(canned),
                tokens_in=120,
                tokens_out=180,
                model=self.model,
            )
    return _M


def _bull_turn(round_index: int) -> dict:
    return {
        "side": "bull",
        "round_index": round_index,
        "position_summary": "Strong fundamentals + accelerating earnings.",
        "points": [
            {
                "claim": "Earnings up 30% YoY.",
                "evidence": "Fundamentals analyst report: revenue $50B vs $38B prior.",
                "cited_sources": ["analyst:fundamentals"],
            }
        ],
        "response_to_opposing": "" if round_index == 1 else "Bear underweighting growth.",
        "confidence": "MEDIUM",
        "cited_sources": ["analyst:fundamentals"],
    }


def _bear_turn(round_index: int) -> dict:
    return {
        "side": "bear",
        "round_index": round_index,
        "position_summary": "Valuation extended; tape weakening.",
        "points": [
            {
                "claim": "Multiple at 35x is rich.",
                "evidence": "Technical analyst RSI=78; fundamentals PE=35.",
                "cited_sources": ["analyst:technical"],
            }
        ],
        "response_to_opposing": "" if round_index == 1 else "Bull ignoring multiples.",
        "confidence": "MEDIUM",
        "cited_sources": ["analyst:technical"],
    }


@pytest.mark.asyncio
async def test_bull_round_1() -> None:
    canned = _bull_turn(1)
    agent = _mock(BullResearcherAgent, canned)(user_id="ariel")
    rep = await agent.run(
        analyst_reports=[
            {"agent_role": "fundamentals", "summary": "growth strong", "cited_sources": ["x"]}
        ],
        prior_rounds=None,
        round_index=1,
        n_max=1,
        ticker="AAPL",
    )
    assert isinstance(rep.output, ResearcherTurn)
    assert rep.output.side == "bull"
    assert rep.output.round_index == 1
    assert rep.output.points
    assert rep.output.cited_sources


@pytest.mark.asyncio
async def test_bear_round_2_responds_to_bull() -> None:
    canned = _bear_turn(2)
    agent = _mock(BearResearcherAgent, canned)(user_id="ariel")
    prior = [_bull_turn(1), _bear_turn(1), _bull_turn(2)]
    rep = await agent.run(
        analyst_reports=[
            {"agent_role": "technical", "rsi": 78, "cited_sources": ["x"]}
        ],
        prior_rounds=prior,
        round_index=2,
        n_max=2,
        ticker="AAPL",
    )
    assert rep.output.response_to_opposing


@pytest.mark.asyncio
async def test_facilitator_extracts_outcome() -> None:
    canned = {
        "winning_side": "bull",
        "synthesis": "Bull case carried with cited fundamentals beats.",
        "cited_evidence": [
            "Earnings up 30% YoY",
            "Revenue $50B (cite: analyst:fundamentals)",
        ],
        "rounds_run": 2,
        "confidence": "MEDIUM",
        "cited_sources": ["analyst:fundamentals"],
    }
    agent = _mock(ResearcherFacilitatorAgent, canned)(user_id="ariel")
    rep = await agent.run(
        bull_turns=[_bull_turn(1), _bull_turn(2)],
        bear_turns=[_bear_turn(1), _bear_turn(2)],
        rounds_run=2,
        ticker="AAPL",
    )
    out: DebateOutcome = rep.output  # type: ignore[assignment]
    assert out.winning_side == "bull"
    assert out.rounds_run == 2
    assert out.cited_evidence


def test_researcher_prompt_carries_round_index() -> None:
    """Smoke: build_prompt mentions the round index, side, and ticker."""
    agent = BullResearcherAgent(user_id="ariel")
    sys, usr = agent.build_prompt(
        analyst_reports=[{"agent_role": "fundamentals"}],
        prior_rounds=[_bull_turn(1)],
        round_index=2,
        n_max=2,
        ticker="AAPL",
    )
    assert "Round 2 of 2" in usr
    assert "bull" in sys
    assert "AAPL" in usr
