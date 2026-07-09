"""Escalation bar — fatal FORKS only (binding rule, 2026-07-09).

Two halves, mirroring the doctrine split:

* PROMPT tests — every escalation-DECIDING agent embeds the bar in its
  system prompt (the agent judges).
* TRANSPORT tests — the deterministic ``same_path_signature`` shape
  check warns on same-unit value comparisons ('12.0% vs 13.0%') and
  stays silent on genuine path forks; it never blocks.
"""

from __future__ import annotations

from argosy.services.escalation_guard import ESCALATION_BAR, same_path_signature


# ---------------------------------------------------------------------------
# Prompt tests — each touched agent carries the bar
# ---------------------------------------------------------------------------


def test_critique_closer_prompt_contains_bar() -> None:
    from argosy.agents.critique_closer import CritiqueCloserAgent

    agent = CritiqueCloserAgent(user_id="test")
    system, _user = agent.build_prompt(
        plan_label="v67",
        plan_markdown="# Plan\nNVDA target 8%.",
        findings_block="[0] RED | nvda_target | prose says 12% vs plan 8%",
        raw_markdown_editable=False,
    )
    assert ESCALATION_BAR in system
    # Closer-specific rule: value disagreements are never needs_user_input.
    assert "NEVER needs_user_input" in system
    assert "dispute" in system
    # The canonical 12-vs-13 non-escalation example rides in the bar.
    assert "12%" in system and "13%" in system


def test_fm_dialogue_verdict_prompt_contains_bar() -> None:
    from argosy.agents.fund_manager_dialogue_verdict import (
        FundManagerDialogueVerdictAgent,
    )

    agent = FundManagerDialogueVerdictAgent(user_id="test")
    system, _user = agent.build_prompt(
        objection_topic="glide pace",
        objection_detail="pace 1,600 vs 4,775 shares YTD",
        objection_severity="red",
        analyst_role="technical",
        analyst_stance="REBUT",
        analyst_reasoning_md="pace is anchored to the tax year",
        analyst_suggested_fix="",
        analyst_cited_sources=["agent_report:PlanCritiqueAgent"],
    )
    assert ESCALATION_BAR in system
    # Verdict-specific rule: value/wording impasse is never ESCALATE_TO_USER.
    assert "NEVER ESCALATE_TO_USER" in system


def test_action_proposer_system_prompt_contains_bar() -> None:
    from argosy.agents.action_proposer import _SYSTEM_PROMPT

    assert ESCALATION_BAR in _SYSTEM_PROMPT
    # Proposer-specific application: internal disagreements never become
    # client-queue proposals.
    assert "reconciliation" in _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Transport guard — deterministic shape check, log-only
# ---------------------------------------------------------------------------


def test_guard_warns_on_same_unit_percent_comparison() -> None:
    assert same_path_signature(
        "The IPS prose says the NVDA target is 12.0% vs 13.0% in the "
        "structured plan — which should we keep?"
    )


def test_guard_silent_on_structural_path_question() -> None:
    assert not same_path_signature(
        "should we sell the NVDA core to fund the sleeve"
    )


def test_guard_warns_on_bare_same_magnitude_numbers() -> None:
    # The FX-assumption class: dashboard 3.00 vs plan 2.944.
    assert same_path_signature("Dashboard shows FX 3.00 vs 2.944 in the plan.")


def test_guard_silent_on_mixed_units() -> None:
    # Percent vs bare number is not a same-path value comparison.
    assert not same_path_signature("allocate 5% vs 40000 available")


def test_guard_silent_on_different_magnitudes() -> None:
    # Order-of-magnitude apart — describing two different quantities.
    assert not same_path_signature("sold 1,600 vs quota is not 16 accounts")
    assert not same_path_signature("100 vs 2 choices remain")


def test_guard_silent_on_empty_and_none() -> None:
    assert not same_path_signature("")
    assert not same_path_signature(None)
