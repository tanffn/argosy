"""Tests for argosy.agents.plan_distiller — see SDD §6.10 / spec §3."""

from __future__ import annotations

from datetime import date

import pytest


def test_plan_distillate_round_trips_minimal():
    """A minimal PlanDistillate must construct + serialize cleanly."""
    from argosy.agents.plan_distiller_types import (
        PlanDistillate,
        Goal,
        Principle,
        Target,
        DecisionRule,
        Constraint,
    )

    d = PlanDistillate(
        plan_label="Jacobs Wealth Plan v2.0",
        distilled_at_iso="2026-05-05T00:00:00+00:00",
        goals=[
            Goal(
                label="retirement_target_year",
                value="2031",
                rationale="Stated retirement target",
                source_section="Executive Overview",
            )
        ],
        principles=[
            Principle(
                label="UCITS-first for estate safety",
                rationale="Avoids US estate exposure for non-resident aliens",
                source_section="Asset Allocation",
            )
        ],
        risk_priorities=["concentration", "fx", "sector_overweight"],
        decision_rules=[
            DecisionRule(
                label="bracket_aware_rsu_sales",
                rule="Spread RSU sales across years to avoid 47-50% bracket spikes",
                source_section="Tax Optimization",
            )
        ],
        targets=[
            Target(
                label="NVDA concentration",
                value=0.15,
                unit="pct_of_portfolio",
                stated_at=date(2026, 2, 1),
                revisit_after=date(2026, 8, 1),
                rationale="Reduce single-stock exposure",
                source_section="Investment Strategy",
            )
        ],
        constraints=[
            Constraint(
                label="no_consolidate_brokers",
                detail="Do not recommend merging Schwab and Leumi",
                source_section="Operational Preferences",
            )
        ],
        stress_tolerance="Willing to ride 30% drawdown while employed",
    )

    payload = d.model_dump_json()
    assert "Jacobs Wealth Plan v2.0" in payload
    assert "concentration" in payload

    # Round-trip
    d2 = PlanDistillate.model_validate_json(payload)
    assert d2.plan_label == d.plan_label
    assert d2.targets[0].unit == "pct_of_portfolio"
    assert d2.risk_priorities[0] == "concentration"


def test_plan_distiller_agent_basic_shape():
    """The agent declares the right role, output model, and citation policy."""
    from argosy.agents.plan_distiller import PlanDistillerAgent
    from argosy.agents.plan_distiller_types import PlanDistillate

    agent = PlanDistillerAgent(user_id="test")
    assert agent.agent_role == "plan_distiller"
    assert agent.output_model is PlanDistillate
    # Source IS the plan -> external citations not required, but the
    # source_section provenance is expected per item.
    assert agent.require_citations is False


def test_plan_distiller_build_prompt_contains_exclusion_list():
    """The system prompt must enumerate excluded categories explicitly."""
    from argosy.agents.plan_distiller import PlanDistillerAgent

    agent = PlanDistillerAgent(user_id="test")
    sys, usr = agent.build_prompt(
        plan_label="Jacobs Wealth Plan v2.0",
        plan_markdown="# Plan\n\nNVDA at 66% today.\n",
    )
    # Exclusion list — these phrases must appear so the agent knows
    # what to drop:
    for phrase in (
        "current portfolio percentages",
        "current FX rates",
        "specific dollar amounts",
        "dated tranche schedules",
        "share counts",
    ):
        assert phrase.lower() in sys.lower(), f"missing exclusion: {phrase}"
    # Plan markdown must be in the user prompt (not the system prompt —
    # makes prompt-cache friendliness easier later).
    assert "NVDA at 66% today" in usr
    # Plan label must be passed through.
    assert "Jacobs Wealth Plan v2.0" in usr
