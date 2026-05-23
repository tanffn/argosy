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
    sys, usr, sources = agent.build_prompt(
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
    # Plan label must be passed through on the user prompt.
    assert "Jacobs Wealth Plan v2.0" in usr
    # Wave A: plan markdown body lives in the Citations API sources list,
    # NOT inlined into the user prompt. The user prompt references the
    # source_id by name.
    assert "NVDA at 66% today" not in usr
    assert "NVDA at 66% today" not in sys
    assert "plan/baseline_markdown" in usr
    assert sources == [("plan/baseline_markdown", "# Plan\n\nNVDA at 66% today.\n")]


@pytest.mark.asyncio
async def test_plan_distiller_run_threads_sources_into_call_model():
    """BaseAgent.run must forward the 3-tuple's sources kwarg to _call_model."""
    import json as _json

    from argosy.agents.base import ModelCall
    from argosy.agents.plan_distiller import PlanDistillerAgent

    captured: dict[str, object] = {}

    class _MockDistiller(PlanDistillerAgent):
        async def _call_model(
            self,
            *,
            system: str,
            user: str,
            sources: list[tuple[str, str]] | None = None,
            **_extra: object,
        ) -> ModelCall:
            captured["system"] = system
            captured["user"] = user
            captured["sources"] = sources
            # Minimal valid PlanDistillate payload — exercises the run path.
            return ModelCall(
                text=_json.dumps({
                    "plan_label": "Test Plan",
                    "distilled_at_iso": "2026-05-23T00:00:00+00:00",
                    "goals": [],
                    "principles": [],
                    "risk_priorities": [],
                    "decision_rules": [],
                    "targets": [],
                    "constraints": [],
                    "stress_tolerance": "",
                }),
                tokens_in=10,
                tokens_out=20,
                model=self.model,
            )

    agent = _MockDistiller(user_id="test")
    report = await agent.run(
        plan_label="Test Plan",
        plan_markdown="# Test Plan\n\nbody here\n",
    )

    assert report.output.plan_label == "Test Plan"
    assert captured["sources"] == [
        ("plan/baseline_markdown", "# Test Plan\n\nbody here\n"),
    ]
    # The plan body must NOT be inlined into the user prompt anymore.
    assert "body here" not in captured["user"]


def test_render_distillate_to_markdown_smoke():
    """Rendered markdown contains every category header and each label."""
    from datetime import date

    from argosy.agents.plan_distiller_render import render_distillate
    from argosy.agents.plan_distiller_types import (
        Constraint,
        DecisionRule,
        Goal,
        PlanDistillate,
        Principle,
        Target,
    )

    d = PlanDistillate(
        plan_label="Jacobs v2.0",
        distilled_at_iso="2026-05-05T00:00:00+00:00",
        goals=[Goal(label="retirement_target_year", value="2031")],
        principles=[Principle(label="UCITS-first")],
        risk_priorities=["concentration", "fx"],
        decision_rules=[DecisionRule(label="bracket_aware_rsu_sales", rule="spread sales")],
        targets=[
            Target(
                label="NVDA concentration",
                value=0.15,
                unit="pct_of_portfolio",
                stated_at=date(2026, 2, 1),
                revisit_after=date(2026, 8, 1),
            )
        ],
        constraints=[Constraint(label="no_consolidate_brokers", detail="keep separate")],
        stress_tolerance="30% drawdown OK while employed",
    )

    md = render_distillate(d)
    assert "# Plan distillate — Jacobs v2.0" in md
    assert "## Goals" in md
    assert "retirement_target_year" in md
    assert "## Principles" in md
    assert "UCITS-first" in md
    assert "## Risk priorities" in md
    assert "concentration" in md
    assert "## Decision rules" in md
    assert "bracket_aware_rsu_sales" in md
    assert "## Targets" in md
    assert "NVDA concentration" in md
    assert "stated 2026-02-01" in md
    assert "revisit 2026-08-01" in md
    assert "## Constraints" in md
    assert "no_consolidate_brokers" in md
    assert "## Stress tolerance" in md
    assert "30% drawdown OK" in md
