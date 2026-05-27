"""DEFAULT_THINKING_BUDGET_BY_ROLE + per-agent resolution."""
from __future__ import annotations

from argosy.agents.base import BaseAgent, DEFAULT_THINKING_BUDGET_BY_ROLE


def test_high_stakes_roles_have_thinking_budgets():
    # Fleet-wide Opus 4.7 + raised thinking budgets (2026-05-27).
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["bull_researcher"] == 8000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["bear_researcher"] == 8000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["trader"] == 8000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["fund_manager"] == 16000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["plan_synthesizer"] == 16000
    assert DEFAULT_THINKING_BUDGET_BY_ROLE["audit"] == 8000


def test_other_roles_default_to_zero():
    """Roles absent from the table get 0 (no thinking)."""
    # `news_analyst` is the wrong key (the agent_role is "news"); the
    # absent-key fallback is still 0. `intake` IS now in the table with
    # 2000, so we check a genuinely-absent key instead.
    assert DEFAULT_THINKING_BUDGET_BY_ROLE.get("news_analyst", 0) == 0
    assert DEFAULT_THINKING_BUDGET_BY_ROLE.get("nonexistent_role", 0) == 0


def test_agent_resolves_its_thinking_budget():
    class _Trader(BaseAgent):
        agent_role = "trader"
        output_model = type("Out", (), {})
        def build_prompt(self, **_): return ("", "")

    class _News(BaseAgent):
        # Note: key "news_analyst" is NOT in the budget table — the real
        # news agent's agent_role is "news". This class exists to test
        # the absent-key fallback path (resolves to 0).
        agent_role = "news_analyst"
        output_model = type("Out", (), {})
        def build_prompt(self, **_): return ("", "")

    assert _Trader(user_id="ariel").thinking_budget == 8000
    assert _News(user_id="ariel").thinking_budget == 0
