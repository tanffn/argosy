"""TraderAgent tests; mock Anthropic."""

from __future__ import annotations

import json
from typing import Any

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.trader import TraderAgent, TraderProposal


def _mock(canned: dict, *, tier: str = "T2"):
    class _M(TraderAgent):
        async def _call_model(self, *, system: str, user: str, **_extra: Any) -> ModelCall:
            return ModelCall(
                text=json.dumps(canned),
                tokens_in=200,
                tokens_out=300,
                model=self.model,
            )

    return _M(user_id="ariel", tier=tier)


_BUY_PROPOSAL = {
    "ticker": "AAPL",
    "action": "buy",
    "size_shares_or_currency": 50.0,
    "size_units": "shares",
    "instrument": "stock",
    "order_type": "limit",
    "limit_price": 200.0,
    "stop_price": None,
    "time_in_force": "DAY",
    "rationale_summary": "Bull case carried; PE attractive vs growth.",
    "expected_impact": {
        "concentration_delta": "AAPL 0% -> 0.3%",
        "cash_delta": "-$10,000",
        "tax_estimate": "no immediate tax",
    },
    "confidence": "MEDIUM",
    "cited_sources": ["analyst:fundamentals", "researcher_facilitator"],
}


@pytest.mark.asyncio
async def test_trader_produces_proposal_t2_default_opus() -> None:
    agent = _mock(_BUY_PROPOSAL, tier="T2")
    assert agent.model == "claude-opus-4-8"
    rep = await agent.run(
        analyst_reports=[
            {"agent_role": "fundamentals", "cited_sources": ["x"]}
        ],
        debate_outcome={"winning_side": "bull", "synthesis": "Strong"},
        positions_snapshot="(no positions)",
        user_constraints="",
        tier="T2",
        ticker="AAPL",
    )
    out: TraderProposal = rep.output  # type: ignore[assignment]
    assert out.action == "buy"
    assert out.ticker == "AAPL"
    assert out.size_shares_or_currency == 50.0
    assert out.cited_sources


@pytest.mark.asyncio
async def test_trader_t0_uses_sonnet() -> None:
    agent = _mock(_BUY_PROPOSAL, tier="T0")
    assert agent.model == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_trader_hold_action() -> None:
    canned = {
        **_BUY_PROPOSAL,
        "action": "hold",
        "size_shares_or_currency": 0.0,
        "rationale_summary": "Insufficient confidence; hold.",
    }
    agent = _mock(canned, tier="T2")
    rep = await agent.run(
        analyst_reports=[{"agent_role": "fundamentals", "cited_sources": ["x"]}],
        debate_outcome={"winning_side": "split", "synthesis": "stalemate"},
        positions_snapshot="",
        user_constraints="",
        tier="T2",
    )
    assert rep.output.action == "hold"


def test_trader_prompt_carries_tier() -> None:
    agent = TraderAgent(user_id="ariel", tier="T3")
    sys, usr = agent.build_prompt(
        analyst_reports=[{"agent_role": "fundamentals"}],
        debate_outcome={"winning_side": "bear"},
        positions_snapshot="(empty)",
        user_constraints="no leverage",
        tier="T3",
        ticker="NVDA",
    )
    assert "T3" in usr
    assert "NVDA" in usr
