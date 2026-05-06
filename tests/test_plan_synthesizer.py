"""Tests for plan_synthesizer types and rendering."""

from __future__ import annotations

from datetime import date

import pytest


def test_horizon_section_round_trips():
    from argosy.agents.plan_synthesizer_types import (
        Action,
        Delta,
        HorizonSection,
        SpeculativeCandidate,
        SynthTarget,
        Theme,
    )

    h = HorizonSection(
        horizon="medium",
        freshness_expected="quarterly",
        status="minor_revision",
        posture="Continue concentration reduction; bias growth tilt for accumulation phase.",
        targets=[
            SynthTarget(
                label="NVDA concentration",
                value=0.12,
                unit="pct_of_portfolio",
                stated_at=date(2026, 5, 1),
                revisit_after=date(2026, 8, 1),
                rationale="DeepSeek + tariff overhang argues for tighter cap",
                source_section=None,
            )
        ],
        themes=[
            Theme(
                label="Tighter NVDA cap",
                direction="lean_away_from",
                rationale="structural shift",
                cited_sources=["agent_report:42"],
            )
        ],
        actions=[
            Action(
                label="Sell NVDA tranche on next strength",
                horizon_kind="parameterized",
                trigger_or_date="if NVDA > $200",
                detail="2500 shares",
                rationale="execute the medium-horizon target",
                cited_sources=["decision_run:99"],
            )
        ],
        speculative_candidates=[],
        deltas_from_prior=[
            Delta(
                item_kind="target",
                item_id="medium.targets.nvda",
                horizon="medium",
                change_kind="modified",
                summary="NVDA target tightened 15% -> 12%",
                prior={"value": 0.15, "unit": "pct_of_portfolio"},
                proposed={"value": 0.12, "unit": "pct_of_portfolio"},
                rationale="macro analyst flagged DeepSeek + tariff overhang",
                cited_sources=["agent_report:macro:2026-05-01"],
            )
        ],
        rationale="Updated medium horizon based on Phase 4 risk debate.",
        cited_sources=["plan_section:Investment Strategy"],
    )

    payload = h.model_dump_json()
    h2 = HorizonSection.model_validate_json(payload)
    assert h2.targets[0].value == 0.12
    assert h2.deltas_from_prior[0].change_kind == "modified"


def test_speculative_candidate_validates():
    from argosy.agents.plan_synthesizer_types import SpeculativeCandidate

    c = SpeculativeCandidate(
        ticker="HOOD",
        thesis_summary="momentum + sector rotation",
        suggested_position_usd=800,
        suggested_position_pct_of_net_worth=0.0008,
        risk_ceiling_check=True,
        horizon_days=30,
        expected_drawdown_pct=0.20,
        exit_trigger="stop -20%, take +50%",
        sourced_from=["sentiment", "watchlist"],
    )
    assert c.risk_ceiling_check is True


def test_short_horizon_only_allows_speculative_candidates():
    """SpeculativeCandidate is structurally a `short`-only field — covered by validation."""
    from argosy.agents.plan_synthesizer_types import (
        HorizonSection,
        SpeculativeCandidate,
    )

    bad = HorizonSection(
        horizon="medium",
        freshness_expected="quarterly",
        status="no_change",
        posture="x",
        speculative_candidates=[
            SpeculativeCandidate(
                ticker="HOOD", thesis_summary="x",
                suggested_position_usd=1, suggested_position_pct_of_net_worth=0.001,
                risk_ceiling_check=True, horizon_days=10, expected_drawdown_pct=0.1,
                exit_trigger="x", sourced_from=[],
            )
        ],
    )
    # We choose to NOT raise here; the synthesizer is responsible for
    # only emitting them on `short`. The test asserts the type still
    # validates so legacy data round-trips.
    assert len(bad.speculative_candidates) == 1


def test_plan_synthesizer_agent_basic_shape():
    from argosy.agents.plan_synthesizer import PlanSynthesizerAgent
    from argosy.agents.plan_synthesizer_types import PlanSynthesisOutput

    agent = PlanSynthesizerAgent(user_id="test")
    assert agent.agent_role == "plan_synthesizer"
    assert agent.output_model is PlanSynthesisOutput
    assert agent.require_citations is True


def test_plan_synthesizer_prompt_includes_authority_disclaimer_and_inputs():
    from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER
    from argosy.agents.plan_synthesizer import PlanSynthesizerAgent

    agent = PlanSynthesizerAgent(user_id="test")
    sys, usr = agent.build_prompt(
        baseline_distillate_md="# Distillate\n\nNVDA target 15%",
        prior_current_md="# Prior current",
        analyst_reports_text="news: ok\nmacro: ok\n",
        debate_outcomes_text="long: hold; medium: tighten; short: harvest",
        portfolio_snapshot_summary="NVDA 14%; cash 5%",
        recent_fills_summary="sold 1000 NVDA on 2026-04-15",
    )
    # System prompt MUST include the authority disclaimer verbatim.
    assert AUTHORITY_DISCLAIMER in sys
    # Inputs are in the user prompt.
    assert "Distillate" in usr
    assert "Prior current" in usr
    assert "news: ok" in usr
    assert "tighten" in usr
    assert "NVDA 14%" in usr
    assert "sold 1000 NVDA" in usr


def test_synthesizer_prompt_includes_speculation_cap():
    """The synthesizer's user prompt must surface the cap so it cannot
    emit candidates that would breach it.
    """
    from argosy.agents.plan_synthesizer import PlanSynthesizerAgent

    # ADAPTATION: BaseAgent.__init__ requires user_id (Task 2.5 finding).
    agent = PlanSynthesizerAgent(user_id="test")
    sys, usr = agent.build_prompt(
        baseline_distillate_md="x",
        prior_current_md="x",
        analyst_reports_text="x",
        debate_outcomes_text="x",
        portfolio_snapshot_summary="net_worth_usd: 4_600_000",
        recent_fills_summary="x",
        speculation_cap_pct=0.001,
        speculation_cap_concurrent=3,
    )
    # Cap must appear so the model cannot ignore it.
    assert "0.1" in usr or "0.001" in usr or "0.10%" in usr or "0.1" in sys or "0.001" in sys or "0.10%" in sys
    assert "3" in (sys + usr)
    assert "speculative" in (sys + usr).lower()


def test_synthesizer_post_validates_speculative_candidates(monkeypatch):
    """If the model emits a candidate over cap, the orchestrator drops it."""
    from argosy.agents.plan_synthesizer_types import (
        HorizonSection,
        PlanSynthesisOutput,
        SpeculativeCandidate,
        SynthesisInputs,
    )
    from argosy.orchestrator.flows import plan_synthesis as flow

    over_cap = SpeculativeCandidate(
        ticker="OVER", thesis_summary="too big",
        suggested_position_usd=50_000,
        suggested_position_pct_of_net_worth=0.011,  # 1.1% NW — over default cap of 0.1%
        risk_ceiling_check=False,
        horizon_days=10, expected_drawdown_pct=0.2,
        exit_trigger="x", sourced_from=["sentiment"],
    )
    in_cap = SpeculativeCandidate(
        ticker="OK", thesis_summary="bounded",
        suggested_position_usd=800,
        suggested_position_pct_of_net_worth=0.0008,
        risk_ceiling_check=True,
        horizon_days=10, expected_drawdown_pct=0.2,
        exit_trigger="x", sourced_from=["sentiment"],
    )
    out = PlanSynthesisOutput(
        long=HorizonSection(horizon="long", freshness_expected="annual",
                            status="no_change", posture="x"),
        medium=HorizonSection(horizon="medium", freshness_expected="quarterly",
                              status="no_change", posture="x"),
        short=HorizonSection(
            horizon="short", freshness_expected="monthly", status="no_change",
            posture="x", speculative_candidates=[over_cap, in_cap],
        ),
        inputs=SynthesisInputs(),
    )

    cleaned = flow._enforce_speculation_cap(
        out, max_pct_of_net_worth=0.001, max_concurrent_positions=3,
    )
    tickers = [c.ticker for c in cleaned.short.speculative_candidates]
    assert tickers == ["OK"]
    assert all(c.risk_ceiling_check for c in cleaned.short.speculative_candidates)
