"""DEFAULT_THINKING_BUDGET_BY_ROLE + per-agent resolution."""
from __future__ import annotations

from argosy.agents.base import BaseAgent, DEFAULT_THINKING_BUDGET_BY_ROLE


def test_high_stakes_roles_have_thinking_budgets():
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["bull_researcher"] == 4000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["bear_researcher"] == 4000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["trader"] == 8000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["fund_manager"] == 8000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["plan_synthesizer"] == 8000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["audit"] == 4000


def test_other_roles_default_to_zero():
    """Non-listed roles get 0 (no thinking)."""
    assert DEFAULT_THINKING_BUDGET_BY_ROLE.get("news_analyst", 0) == 0
    assert DEFAULT_THINKING_BUDGET_BY_ROLE.get("intake", 0) == 0


def test_agent_resolves_its_thinking_budget():
    class _Trader(BaseAgent):
        agent_role = "trader"
        output_model = type("Out", (), {})
        def build_prompt(self, **_): return ("", "")

    class _News(BaseAgent):
        agent_role = "news_analyst"
        output_model = type("Out", (), {})
        def build_prompt(self, **_): return ("", "")

    assert _Trader(user_id="ariel").thinking_budget == 8000
    assert _News(user_id="ariel").thinking_budget == 0
