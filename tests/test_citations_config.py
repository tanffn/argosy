"""DEFAULT_CITATIONS_BY_ROLE + per-agent resolution."""
from __future__ import annotations

from argosy.agents.base import BaseAgent, DEFAULT_CITATIONS_BY_ROLE


def test_source_consumers_have_citations_enabled():
    # Keys MUST match each subclass's `agent_role` class attribute
    # (the lookup in BaseAgent.__init__ is keyed by agent_role). Earlier
    # drafts of this test referenced "news_analyst" — the news agent's
    # role is actually "news" (see NewsAnalystAgent.agent_role) and the
    # mismatch silently disabled citations on the live path. Task 20
    # (live analyst integration) caught it.
    for role in (
        "news", "fundamentals", "technical", "sentiment",
        "macro", "tax", "fx", "intake_extractor", "plan_distiller",
        "plan_critique", "concentration",
    ):
        assert DEFAULT_CITATIONS_BY_ROLE[role] is True, role


def test_synthesizers_have_citations_enabled():
    for role in (
        "bull_researcher", "bear_researcher",
        "trader", "fund_manager", "audit", "plan_synthesizer",
    ):
        assert DEFAULT_CITATIONS_BY_ROLE[role] is True, role


def test_non_source_agents_have_citations_disabled():
    for role in (
        "advisor", "intake", "household_categorizer",
        "researcher_facilitator", "risk_facilitator",
        "domain_refresh", "watchlist",
    ):
        assert DEFAULT_CITATIONS_BY_ROLE[role] is False, role


def test_agent_resolves_citations_flag():
    # Use the canonical role name "news" (matches NewsAnalystAgent.agent_role).
    class _News(BaseAgent):
        agent_role = "news"
        output_model = type("Out", (), {})
        def build_prompt(self, **_): return ("", "")

    class _Advisor(BaseAgent):
        agent_role = "advisor"
        output_model = type("Out", (), {})
        def build_prompt(self, **_): return ("", "")

    assert _News(user_id="ariel").citations_enabled is True
    assert _Advisor(user_id="ariel").citations_enabled is False
