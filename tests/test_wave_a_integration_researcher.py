"""Live-LLM integration test — Wave A researcher family (extended thinking).

Marked ``llm_eval`` (opt-in via ``-m llm_eval``). Fires a single live
BullResearcherAgent call against the real LLM backend, with the per-role
thinking budget (4000 tokens) configured via
``DEFAULT_THINKING_BUDGET_BY_ROLE``.

Asserts:
  1. The agent picks up ``thinking_budget == 4000`` from the per-role
     config (regression guard: someone could quietly remove the
     ``bull_researcher`` entry from ``DEFAULT_THINKING_BUDGET_BY_ROLE``
     without breaking any other test).
  2. The live model actually USES the thinking budget — i.e. the report
     records ``thinking_tokens > 0`` (the call materialized through
     ``_call_via_api_key`` with the ``thinking`` param wired in by
     Task 11, AND the SDK surfaced ``usage.thinking_tokens`` per Task 12).
  3. The recorded ``thinking_tokens`` stays within the budget cap
     (the API must not exceed what we asked for).

Cost: ~$0.30-1.00 per run on Opus 4.7 with a 4000-token thinking budget
and ~3 small analyst reports as input. Authorized by the Wave A plan
(Task 22).

The mock analyst reports are built INLINE as pydantic models per the
task brief — the researcher is a synthesizer that reads upstream analyst
outputs, so we just need three plausible-looking dicts to feed
``build_prompt``.
"""

from __future__ import annotations

import pytest

from argosy.agents.researcher import BullResearcherAgent, ResearcherTurn

# Shared helper lifted to conftest in Wave A finalization (Issue 2) so all
# Wave A live tests share one definition. The api_key backend is the only
# one that surfaces ``usage.thinking_tokens`` on the SDK response — the
# claude_code path's ResultMessage usage dict does not expose it — so this
# test SKIPS cleanly when the configured backend is claude_code.
from tests.conftest import _api_key_backend_available  # noqa: E402


def _mock_analyst_reports() -> list[dict]:
    """Three small analyst-shaped dicts the researcher reads as input.

    Mirrors the shape produced by the real analyst agents
    (``agent_role`` + a content payload + ``cited_sources``), but with
    short, illustrative text so the researcher has something concrete to
    reason about without burning tokens on the analyst layer itself.
    """
    return [
        {
            "agent_role": "fundamentals",
            "summary": (
                "NVDA trades at forward P/E ~38 vs the 5y average of ~32. "
                "Datacenter revenue grew 154% YoY last quarter to $26B and now "
                "represents 87% of total revenue. Gross margin held at 75%. "
                "Free cash flow conversion remains above 50% of net income."
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
                "NVDA closed at $890, up 1.2% on the session. RSI is at 64 "
                "(neutral, not overbought). The 50-day SMA at $865 is acting "
                "as support; the 200-day SMA at $720 is the major lower pivot. "
                "Volume tracking the 30-day average."
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
                "Sell-side sentiment is overwhelmingly positive: 42 of 48 "
                "covering analysts at Buy, median 12-mo PT of $1,050. "
                "Recent news cycle dominated by Blackwell ramp commentary. "
                "Insider selling has picked up modestly per Form 4 filings."
            ),
            "cited_sources": [
                "analyst:sentiment",
                "domain_knowledge/news_taxonomy.md",
            ],
            "confidence": "MEDIUM",
        },
    ]


@pytest.mark.llm_eval
@pytest.mark.skipif(
    not _api_key_backend_available(),
    reason=(
        "Wave A thinking_tokens telemetry is only emitted by the api_key "
        "backend (see BaseAgent._call_via_api_key + Task 12). Set "
        "ARGOSY_ANTHROPIC__BACKEND=api_key with ANTHROPIC_API_KEY (or "
        "keychain entry) to run this test."
    ),
)
def test_bull_researcher_thinking_active() -> None:
    """Live: bull researcher fires with extended thinking enabled."""
    agent = BullResearcherAgent(user_id="ariel")

    # Assertion 1: per-role thinking budget is picked up from config.
    assert agent.thinking_budget == 4000, (
        f"BullResearcherAgent should default to thinking_budget=4000 "
        f"from DEFAULT_THINKING_BUDGET_BY_ROLE; got {agent.thinking_budget}"
    )

    # Fire the agent live.
    report = agent.run_sync(
        analyst_reports=_mock_analyst_reports(),
        prior_rounds=None,
        round_index=1,
        n_max=2,
        ticker="NVDA",
    )

    # Sanity: the output validates as a ResearcherTurn.
    assert isinstance(report.output, ResearcherTurn)
    assert report.output.side == "bull"
    assert report.output.round_index == 1

    # Assertion 2: thinking was actually used.
    print(
        f"\n[bull_researcher live] model={report.model} "
        f"tokens_in={report.tokens_in} tokens_out={report.tokens_out} "
        f"thinking_tokens={report.thinking_tokens} "
        f"cost_usd=${report.cost_usd:.4f}"
    )
    assert report.thinking_tokens > 0, (
        f"Expected the live model to emit thinking tokens with "
        f"budget={agent.thinking_budget}, but thinking_tokens={report.thinking_tokens}. "
        f"Likely causes: (a) backend is claude_code (does not expose "
        f"thinking_tokens); (b) model rejected the thinking param and "
        f"fell back; (c) SDK shape changed."
    )

    # Assertion 3: thinking stays within the budget cap.
    assert report.thinking_tokens <= agent.thinking_budget, (
        f"thinking_tokens={report.thinking_tokens} exceeds budget="
        f"{agent.thinking_budget}; API should never overshoot."
    )
