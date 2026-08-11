"""Falsifier-authoring evidence-contract rule (task #35, 2026-08-11).

Two independent reviewers (Sol + Claude) named the SAME non-blocking gap: the
trader's falsifiers need an EVIDENCE CONTRACT and, on a SELL, must distinguish
true FALSIFIERS (stop reducing) from downside ACCELERANTS (reduce faster). This
is an LLM-TEAM lever — the fix is the TRADER PROMPT (``_FALSIFIER_RULE``), not a
deterministic gate. These tests assert the guidance is PRESENT in both prompt
branches (the LLM output quality itself is validated by live e2e, not here), and
that the optional ``RevisitTrigger.decision_transition`` field is
backward-compatible.
"""
from __future__ import annotations

import pytest

from argosy.agents.trader import RevisitTrigger, TraderAgent


@pytest.fixture
def agent():
    return TraderAgent(user_id="ariel", tier="T2")


def _system(agent, mode):
    system, _ = agent.build_prompt(
        analyst_reports=[{"agent_role": "fundamentals", "summary": "intact"}],
        debate_outcome={"winner": "bull"},
        positions_snapshot="CURRENT POSITION — NVDA: HELD 10,940 shares.",
        user_constraints="no standing stance here",
        ticker="NVDA",
        mode=mode,
    )
    return system


@pytest.mark.parametrize("mode", ["tactical_trade", "long_hold"])
def test_evidence_contract_present(agent, mode):
    system = _system(agent, mode)
    assert "EVIDENCE CONTRACT" in system
    # The six contract elements.
    assert "SOURCE" in system
    assert "AS-OF DATE" in system
    assert "CURRENT BASELINE" in system
    assert "THRESHOLD" in system
    assert "PERSISTENCE" in system
    assert "DECISION TRANSITION" in system


@pytest.mark.parametrize("mode", ["tactical_trade", "long_hold"])
def test_sell_falsifier_vs_accelerant_distinction(agent, mode):
    system = _system(agent, mode)
    assert "STOP REDUCING" in system
    assert "SELL->PAUSE" in system
    assert "SELL->ACCELERATE" in system
    assert "ACCELERANTS" in system
    assert "MUST be LABELED as accelerants" in system
    assert "at least one true " in system and "PAUSE-falsifier" in system


@pytest.mark.parametrize("mode", ["tactical_trade", "long_hold"])
def test_etf_structural_guidance_present(agent, mode):
    system = _system(agent, mode)
    assert "ETF / FUND FALSIFIERS NEED A NUMERIC THRESHOLD" in system
    assert "STRUCTURAL:" in system


@pytest.mark.parametrize("mode", ["tactical_trade", "long_hold"])
def test_fiscal_label_and_migration_guidance_present(agent, mode):
    system = _system(agent, mode)
    assert "Q2 FY2027" in system
    assert "FY26 Q2" in system  # cited as the WRONG label
    assert "do NOT cancel the planned migration" in system
    assert "SCHD->FUSA" in system


@pytest.mark.parametrize("mode", ["tactical_trade", "long_hold"])
def test_decision_transition_declared_on_trigger(agent, mode):
    system = _system(agent, mode)
    assert "decision_transition" in system


@pytest.mark.parametrize("mode", ["tactical_trade", "long_hold"])
def test_advisor_voice_rationale_guidance_present(agent, mode):
    """Ariel's live-cards feedback (2026-08-11): rationale_summary must read like
    an advisor to a client — UNDER 100 WORDS, no section scaffold, specifics in
    the falsifiers. Present in BOTH prompt branches."""
    system = _system(agent, mode)
    assert "ADVISOR SPEAKING TO THE CLIENT" in system
    assert "UNDER 100 WORDS" in system
    # The old sectioned scaffold is explicitly forbidden.
    assert "NO multi-section scaffold" in system
    # Specifics belong in falsifiers, not the prose.
    assert "live in the FALSIFIERS" in system
    # Citations move to the list field, not inline prose.
    assert "cited_sources" in system


@pytest.mark.parametrize("mode", ["tactical_trade", "long_hold"])
def test_no_section_scaffold_headers_instructed(agent, mode):
    """Guard against a regression that re-introduces the 'Quality read / Price
    read / …' scaffold as REQUIRED. Any mention must be in the FORBIDDEN sense."""
    system = _system(agent, mode)
    # 'AS MARKDOWN SECTIONS' was the old required-scaffold directive — gone now.
    assert "AS MARKDOWN\n" not in system
    assert "AS MARKDOWN " not in system


def test_revisit_trigger_optional_transition_backward_compatible():
    # Without the field — existing shape still validates, key absent on dump.
    t0 = RevisitTrigger(kind="metric_condition", metric="fcf", op="<", value=0.0)
    d0 = t0.model_dump(exclude_none=True)
    assert "decision_transition" not in d0
    assert t0.decision_transition is None

    # With the field — validates and round-trips into the serialized dict.
    t1 = RevisitTrigger(
        kind="metric_condition",
        metric="nvda_concentration_pct",
        op="<",
        value=13.0,
        decision_transition="SELL->PAUSE",
    )
    d1 = t1.model_dump(exclude_none=True)
    assert d1["decision_transition"] == "SELL->PAUSE"
