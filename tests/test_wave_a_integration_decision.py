"""Live-LLM integration test — Wave A decision family (trader + fund manager).

Marked ``llm_eval`` (opt-in via ``-m llm_eval``). Fires two live Opus 4.7
calls — one TraderAgent, one FundManagerAgent — against the real LLM
backend, each with the per-role thinking budget (8000 tokens) configured
via ``DEFAULT_THINKING_BUDGET_BY_ROLE`` and citations enabled per
``DEFAULT_CITATIONS_BY_ROLE``.

Asserts (per the Wave A plan, Task 23):
  - trader  : ``thinking_budget == 8000`` AND ``citations_enabled is True``
              AND live ``report.thinking_tokens > 0`` AND, when the model
              emitted citations, the JSON is a non-empty list.
  - fund_mgr: ``thinking_budget == 8000`` AND live ``report.thinking_tokens
              > 0`` AND ``report.cost_usd > 0``.

Cost: ~$1-3 per run on the api_key backend (two Opus calls with 8000-
token thinking budgets and a few KB of structured inputs each).
Authorized by the Wave A plan.

The mock inputs are built INLINE as plain dicts per the task brief — both
agents are synthesizers that read upstream outputs; chaining through real
analyst / debate / risk agents to produce them would balloon cost and
introduce variability unrelated to the trader / FM call itself.
"""

from __future__ import annotations

import json

import pytest

from argosy.agents.base import _llm_backend_available
from argosy.agents.fund_manager import FundManagerAgent, FundManagerDecision
from argosy.agents.trader import TraderAgent, TraderProposal

# Wave A finalization (Issue 2): both trader and fund_manager assert on
# ``thinking_tokens > 0``, which the claude_code backend cannot surface
# (its ResultMessage.usage dict carries no ``thinking_tokens`` field).
# Skip cleanly when the api_key backend isn't reachable rather than
# live-failing the assertion.
from tests.conftest import _api_key_backend_available  # noqa: E402


# ---------------------------------------------------------------------------
# Inline fixtures — minimal mock inputs for the synthesizers.
# ---------------------------------------------------------------------------


def _mock_analyst_reports() -> list[dict]:
    """Three small analyst-shaped dicts the trader reads as input."""
    return [
        {
            "agent_role": "fundamentals",
            "summary": (
                "NVDA forward P/E ~38 vs 5y avg ~32. Datacenter revenue "
                "+154% YoY to $26B (87% of total). Gross margin 75%, "
                "FCF conversion >50% of net income."
            ),
            "cited_sources": [
                "analyst:fundamentals",
                "domain_knowledge/equity_basics.md",
            ],
            "confidence": "MEDIUM",
        },
        {
            "agent_role": "technical",
            "summary": (
                "NVDA closed $890, +1.2% session. RSI 64 (neutral). "
                "50-day SMA $865 acts as support; 200-day SMA $720 is "
                "the major lower pivot. Volume in line with 30-day avg."
            ),
            "cited_sources": [
                "analyst:technical",
                "domain_knowledge/ta_indicators.md",
            ],
            "confidence": "MEDIUM",
        },
        {
            "agent_role": "sentiment",
            "summary": (
                "Sell-side overwhelmingly positive: 42/48 Buy, median PT "
                "$1,050. News cycle dominated by Blackwell ramp. Modest "
                "insider selling per Form 4 filings."
            ),
            "cited_sources": [
                "analyst:sentiment",
                "domain_knowledge/news_taxonomy.md",
            ],
            "confidence": "MEDIUM",
        },
    ]


def _mock_debate_outcome() -> dict:
    """Researcher facilitator-style summary the trader synthesizes from."""
    return {
        "winning_side": "bull",
        "synthesis": (
            "Bull case carries on near-term datacenter demand and margin "
            "resilience. Bear's concentration concern is acknowledged but "
            "scoped: trader should size the position so the post-fill "
            "NVDA weight stays under the 35% sector concentration cap."
        ),
        "key_points": [
            "Datacenter demand inflection (Blackwell ramp) is the dominant "
            "near-term driver per the fundamentals report.",
            "Technical setup is constructive but not euphoric (RSI 64).",
            "Insider selling is a known caveat — not disqualifying.",
        ],
        "cited_sources": ["bull_researcher", "bear_researcher"],
    }


def _mock_positions_snapshot() -> str:
    return (
        "Cash: $50,000. NVDA: 0 shares. Total portfolio: $500,000 "
        "across SPY (55%), QQQ (25%), cash (10%), other (10%)."
    )


def _mock_user_constraints() -> str:
    return (
        "Target NVDA allocation: 0-3% of portfolio. Tax: Israeli "
        "resident, 25% CGT. Prefer limit orders. Single trade size "
        "$2,000-$5,000. Max sector concentration 35%."
    )


def _mock_trader_proposal() -> dict:
    """Trader-output-shaped dict the fund manager validates."""
    return {
        "ticker": "NVDA",
        "action": "buy",
        "size_shares_or_currency": 4.0,
        "size_units": "shares",
        "instrument": "stock",
        "order_type": "limit",
        "limit_price": 890.0,
        "stop_price": None,
        "time_in_force": "DAY",
        "rationale_summary": (
            "Bull case carried debate; size sub-1% of portfolio so post-"
            "fill weight stays well under sector cap. Limit at last close."
        ),
        "expected_impact": {
            "concentration_delta": "NVDA 0% -> 0.7%",
            "cash_delta": "-$3,560",
            "tax_estimate": "no immediate tax (entry trade)",
        },
        "confidence": "MEDIUM",
        "cited_sources": ["researcher_facilitator", "analyst:fundamentals"],
    }


def _mock_risk_outcome() -> dict:
    return {
        "consensus_verdict": "APPROVE",
        "officers": {
            "concentration": "APPROVE",
            "execution":     "APPROVE",
            "tax":           "APPROVE",
        },
        "notes": (
            "Position sizing keeps concentration well below the 35% sector "
            "cap. Limit order is appropriate. No tax friction on entry."
        ),
        "cited_sources": ["risk_facilitator"],
    }


def _mock_plan_critique() -> dict:
    return {
        "findings": [
            {
                "severity": "GREEN",
                "topic": "Concentration",
                "note": (
                    "Proposed sizing is consistent with the durable plan's "
                    "single-name cap policy."
                ),
            }
        ],
        "cited_sources": ["plan_critique:GREEN"],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.llm_eval
@pytest.mark.skipif(
    not _llm_backend_available(),
    reason=(
        "No LLM backend reachable: set ANTHROPIC_API_KEY (api_key backend) "
        "or ensure claude.exe is on PATH (claude_code backend)"
    ),
)
@pytest.mark.skipif(
    not _api_key_backend_available(),
    reason=(
        "Trader live test asserts on thinking_tokens > 0, which the "
        "claude_code SDK does not surface on ResultMessage.usage. Requires "
        "ARGOSY_ANTHROPIC__BACKEND=api_key plus ANTHROPIC_API_KEY (or a "
        "configured keychain entry) to be meaningful."
    ),
)
def test_trader_thinking_and_citations() -> None:
    """Live: trader fires with extended thinking + citations enabled."""
    agent = TraderAgent(user_id="ariel")

    # Per-role defaults are picked up from DEFAULT_THINKING_BUDGET_BY_ROLE
    # and DEFAULT_CITATIONS_BY_ROLE.
    assert agent.thinking_budget == 8000, (
        f"TraderAgent should default to thinking_budget=8000 from "
        f"DEFAULT_THINKING_BUDGET_BY_ROLE; got {agent.thinking_budget}"
    )
    assert agent.citations_enabled is True, (
        f"TraderAgent should default to citations_enabled=True from "
        f"DEFAULT_CITATIONS_BY_ROLE; got {agent.citations_enabled}"
    )

    report = agent.run_sync(
        analyst_reports=_mock_analyst_reports(),
        debate_outcome=_mock_debate_outcome(),
        positions_snapshot=_mock_positions_snapshot(),
        user_constraints=_mock_user_constraints(),
        tier="T2",
        ticker="NVDA",
    )

    # Sanity: structured output validates.
    assert isinstance(report.output, TraderProposal)
    assert report.output.ticker.upper() == "NVDA"
    assert report.output.cited_sources, (
        "TraderProposal.cited_sources must be non-empty (citation gate)."
    )

    print(
        f"\n[trader live] model={report.model} "
        f"tokens_in={report.tokens_in} tokens_out={report.tokens_out} "
        f"thinking_tokens={report.thinking_tokens} "
        f"cost_usd=${report.cost_usd:.4f}"
    )

    # Thinking actually used.
    assert report.thinking_tokens > 0, (
        f"Expected the live model to emit thinking tokens with "
        f"budget={agent.thinking_budget}, but thinking_tokens="
        f"{report.thinking_tokens}. Likely causes: (a) backend is "
        f"claude_code (does not expose thinking_tokens); (b) model "
        f"rejected the thinking param and fell back; (c) SDK shape changed."
    )
    assert report.thinking_tokens <= agent.thinking_budget, (
        f"thinking_tokens={report.thinking_tokens} exceeds budget="
        f"{agent.thinking_budget}; API should never overshoot."
    )

    # Citations: when the API surfaced any, the JSON must parse to a
    # non-empty list. We don't HARD-require citations on a synthesizer
    # because the trader's inputs aren't passed through as document blocks
    # in this minimal-mock harness — but if the API emitted them, they
    # must be well-formed.
    if report.citations_json:
        citations = json.loads(report.citations_json)
        assert isinstance(citations, list)
        assert len(citations) > 0, (
            "citations_json is non-null but parses to an empty list; "
            "citation extraction is broken."
        )


@pytest.mark.llm_eval
@pytest.mark.skipif(
    not _llm_backend_available(),
    reason=(
        "No LLM backend reachable: set ANTHROPIC_API_KEY (api_key backend) "
        "or ensure claude.exe is on PATH (claude_code backend)"
    ),
)
@pytest.mark.skipif(
    not _api_key_backend_available(),
    reason=(
        "Fund-manager live test asserts on thinking_tokens > 0, which the "
        "claude_code SDK does not surface on ResultMessage.usage. Requires "
        "ARGOSY_ANTHROPIC__BACKEND=api_key plus ANTHROPIC_API_KEY (or a "
        "configured keychain entry) to be meaningful."
    ),
)
def test_fund_manager_full_loop() -> None:
    """Live: fund manager fires with extended thinking enabled."""
    agent = FundManagerAgent(user_id="ariel")

    assert agent.thinking_budget == 8000, (
        f"FundManagerAgent should default to thinking_budget=8000 from "
        f"DEFAULT_THINKING_BUDGET_BY_ROLE; got {agent.thinking_budget}"
    )

    report = agent.run_sync(
        proposal=_mock_trader_proposal(),
        risk_outcome=_mock_risk_outcome(),
        plan_critique=_mock_plan_critique(),
        user_constraints=_mock_user_constraints(),
        tier="T2",
    )

    # Sanity: structured output validates.
    assert isinstance(report.output, FundManagerDecision)
    assert report.output.decision in ("green_light", "block")
    assert report.output.cited_sources, (
        "FundManagerDecision.cited_sources must be non-empty (citation gate)."
    )

    print(
        f"\n[fund_manager live] model={report.model} "
        f"tokens_in={report.tokens_in} tokens_out={report.tokens_out} "
        f"thinking_tokens={report.thinking_tokens} "
        f"cost_usd=${report.cost_usd:.4f}"
    )

    # Thinking actually used.
    assert report.thinking_tokens > 0, (
        f"Expected the live model to emit thinking tokens with "
        f"budget={agent.thinking_budget}, but thinking_tokens="
        f"{report.thinking_tokens}. Likely causes: (a) backend is "
        f"claude_code (does not expose thinking_tokens); (b) model "
        f"rejected the thinking param and fell back; (c) SDK shape changed."
    )
    assert report.thinking_tokens <= agent.thinking_budget, (
        f"thinking_tokens={report.thinking_tokens} exceeds budget="
        f"{agent.thinking_budget}; API should never overshoot."
    )

    # Cost was tracked. _estimate_usd is non-zero whenever the call
    # consumes either input or output tokens at the Opus rate; this is a
    # cheap regression guard against accidentally zeroing cost telemetry.
    assert report.cost_usd > 0, (
        f"Expected cost_usd > 0 for a live Opus call; got "
        f"cost_usd={report.cost_usd}. Likely _estimate_usd lost the model "
        f"price entry or telemetry is zeroed."
    )
