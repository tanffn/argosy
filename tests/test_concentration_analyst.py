"""ConcentrationAnalystAgent tests. Mock the Anthropic client."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from argosy.agents.base import ModelCall
from argosy.agents.concentration_analyst import (
    Breach,
    ConcentrationAnalystAgent,
    ConcentrationReport,
)
from argosy.agents.concentration_analyst_types import (
    ConcentrationAnalystOutput,
    ConstraintRow,
    DelaySensitivityRow,
)


class _MockConcentrationAgent(ConcentrationAnalystAgent):
    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output
        self._last_sources: list[tuple[str, str]] | None = None

    async def _call_model(
        self,
        *,
        system: str,
        user: str,
        sources: list[tuple[str, str]] | None = None,
        **_extra: object,
    ) -> ModelCall:
        # Capture the sources kwarg so tests can assert BaseAgent.run
        # forwards the 3-tuple's third element into the model call.
        self._last_sources = sources
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=120,
            tokens_out=140,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_concentration_report_shape() -> None:
    canned = {
        "breaches": [
            {
                "category": "NVDA",
                "actual_pct": 68.0,
                "cap_pct": 25.0,
                "severity": "breach",
                "note": "NVDA single-position cap exceeded by 43pp.",
            }
        ],
        "deltas_vs_target": {"NVDA": 53.0, "Growth": -5.0},
        "nvda_pace": {
            "shares_sold_ytd": 2000,
            "target_shares_ytd": 4000,
            "delta_shares": -2000,
            "on_track": False,
        },
        "summary": "NVDA way over cap; pace behind plan.",
        "confidence": "HIGH",
        "cited_sources": ["TSV 26-May", "Jacobs_Wealth_Plan v2.0"],
    }
    agent = _MockConcentrationAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        positions_summary="NVDA 11,471 shares × $200 ≈ $2.296M (~68% of liquid).",
        plan_targets={"NVDA": 15.0, "Growth": 20.0},
        nvda_shares_sold_ytd=2000,
        nvda_target_shares_ytd=4000,
    )
    out = report.output
    assert isinstance(out, ConcentrationReport)
    assert len(out.breaches) == 1
    assert isinstance(out.breaches[0], Breach)
    assert out.nvda_pace.on_track is False
    assert out.deltas_vs_target["NVDA"] == 53.0
    assert out.cited_sources
    # BaseAgent.run unpacks the build_prompt 3-tuple and forwards sources
    # into _call_model so the Citations API path receives document blocks.
    assert agent._last_sources is not None
    source_ids = [sid for sid, _ in agent._last_sources]
    assert "portfolio/holdings" in source_ids
    assert "plan/targets" in source_ids


def test_build_prompt_returns_sources_tuple() -> None:
    """build_prompt returns (system, user, sources) with holdings + plan extracted."""
    agent = ConcentrationAnalystAgent(user_id="ariel")
    positions_summary = (
        "NVDA 11,471 shares × $200 ≈ $2.296M (~68% of liquid)."
    )
    plan_targets = {"NVDA": 15.0, "Growth": 20.0}

    result = agent.build_prompt(
        positions_summary=positions_summary,
        plan_targets=plan_targets,
        nvda_shares_sold_ytd=2000,
        nvda_target_shares_ytd=4000,
    )
    assert len(result) == 3
    system, user, sources = result

    # User prompt references source_ids but NO longer inlines the bodies.
    assert "portfolio/holdings" in user
    assert "plan/targets" in user
    assert "11,471 shares" not in user
    assert "target 15.0%" not in user
    # NVDA pace scalars remain inline.
    assert "shares_sold_ytd: 2000" in user
    assert "target_shares_ytd: 4000" in user

    # Sources carry the bodies in the documented order.
    source_ids = [sid for sid, _ in sources]
    assert source_ids == ["portfolio/holdings", "plan/targets"]
    bodies = dict(sources)
    assert "11,471 shares" in bodies["portfolio/holdings"]
    assert "NVDA: target 15.0%" in bodies["plan/targets"]
    assert "Growth: target 20.0%" in bodies["plan/targets"]

    # System prompt mentions the document source_ids.
    assert "portfolio/holdings" in system
    assert "plan/targets" in system


def test_build_prompt_empty_inputs_returns_no_sources() -> None:
    """No positions summary + no plan targets → sources == []."""
    agent = ConcentrationAnalystAgent(user_id="ariel")
    system, user, sources = agent.build_prompt(
        positions_summary="",
        plan_targets={},
    )
    assert sources == []
    assert "no positions summary supplied" in user
    assert "no plan targets supplied" in user
    # System prompt names the role + the derivation contract.
    assert "concentration analyst" in system
    assert "MIN(" in system
    assert "sequence_cap" in system
    assert "tail_loss_cap" in system


# ---------------------------------------------------------------------------
# Derivation contract — Codex Q9 + R3 verdict
# ---------------------------------------------------------------------------


def test_system_prompt_derivation_contract() -> None:
    """System prompt encodes the MIN-of-four-constraints derivation."""
    agent = ConcentrationAnalystAgent(user_id="ariel")
    system, _user, _sources = agent.build_prompt(
        positions_summary="NVDA 11,471 shares × $200 ≈ $2.296M.",
        plan_targets={"NVDA": 15.0},
    )
    # MIN(sequence_cap, tail_loss_cap, risk_contribution_cap, tax_liquidity_cap)
    assert "MIN(" in system
    for constraint_name in (
        "sequence_cap",
        "tail_loss_cap",
        "risk_contribution_cap",
        "tax_liquidity_cap",
    ):
        assert constraint_name in system, f"{constraint_name} missing from prompt"
    # The hard rule on never accepting a target_pct from the synth.
    assert "target_pct" in system or "target weights" in system
    assert "FORBIDDEN" in system or "IGNORE" in system or "ignore it" in system


def test_build_prompt_passes_derivation_payloads() -> None:
    """Optional derivation kwargs land in the user-prompt DATA blocks."""
    agent = ConcentrationAnalystAgent(user_id="ariel")
    _system, user, _sources = agent.build_prompt(
        positions_summary="NVDA 11471 shares",
        plan_targets={"NVDA": 15.0},
        sigma_payload={"NVDA_sigma_annual": 0.55},
        correlation_payload={"NVDA_to_core": 0.62},
        tax_payload={"effective_cgt": 0.30},
        withdrawal_payload={"fi_year_p50": 2031, "fi_year_p90": 2034},
        equity_comp_payload={"five_year_avg_net_nis": 500000},
        user_risk_tolerance={"fi_delay_years": 1.0, "max_drawdown_pct": 0.25},
        fx_payload={"USD_NIS": 3.65},
        nvda_share_count=11471,
        nvda_price_usd=200.0,
    )
    # All payload wrappers present.
    for wrapper in (
        "<sigma_payload>",
        "<correlation_payload>",
        "<tax_payload>",
        "<withdrawal_payload>",
        "<equity_comp_payload>",
        "<user_risk_tolerance>",
        "<fx_payload>",
        "<nvda_share_count>",
        "<nvda_price_usd>",
    ):
        assert wrapper in user, f"{wrapper} missing from user prompt"
    # Scalar fallbacks rendered inline.
    assert "11471" in user
    assert "200.00 USD" in user
    # JSON-dumped payloads carry their key strings.
    assert "NVDA_sigma_annual" in user
    assert "effective_cgt" in user
    assert "fi_year_p90" in user
    # Plan targets block carries the DO-NOT-COPY banner.
    assert "DO NOT copy" in user or "informational only" in user.lower()


def test_build_prompt_missing_derivation_payloads_render_placeholders() -> None:
    """Absent derivation kwargs render as 'no … supplied' placeholders."""
    agent = ConcentrationAnalystAgent(user_id="ariel")
    _system, user, _sources = agent.build_prompt(
        positions_summary="NVDA 11471 shares",
        plan_targets={},
    )
    assert "no sigma_payload supplied" in user
    assert "no tax_payload supplied" in user
    assert "no withdrawal_payload supplied" in user
    assert "no user_risk_tolerance supplied" in user
    # Scalar fallbacks default to "(not supplied)".
    assert "<nvda_share_count>" in user
    assert "(not supplied)" in user


def test_concentration_analyst_output_validates_full_derivation() -> None:
    """The new Pydantic schema accepts a complete 4-constraint output."""
    payload = {
        "current_nvda_pct": 0.68,
        "current_risk_contribution_pct": 0.78,
        "tail_loss_p5_1y_pct": 0.27,
        "constraints": [
            {
                "name": "sequence_cap",
                "value_pct": 0.18,
                "derivation_md": (
                    "1-year p5 NVDA shock pushes FI from 2031 to 2032 "
                    "at 18% cap given σ=0.55 + savings rate ₪500k/yr."
                ),
                "confidence": "HIGH",
            },
            {
                "name": "tail_loss_cap",
                "value_pct": 0.22,
                "derivation_md": (
                    "p5 portfolio loss = 25% at NVDA=22% (lognormal "
                    "tail using sigma_calibrator σ_NVDA=0.55)."
                ),
                "confidence": "HIGH",
            },
            {
                "name": "risk_contribution_cap",
                "value_pct": 0.25,
                "derivation_md": (
                    "Marginal-variance contribution hits 30% cap at "
                    "NVDA=25% with ρ=0.62 + σ_core=0.18."
                ),
                "confidence": "MEDIUM",
            },
            {
                "name": "tax_liquidity_cap",
                "value_pct": 0.35,
                "derivation_md": (
                    "Per-year sale capacity ~3000 shares × $200 = $600k "
                    "before surtax-band 30% bite. Caps net realisation."
                ),
                "confidence": "MEDIUM",
            },
        ],
        "nvda_cap_pct": 0.18,
        "delay_sensitivities": [
            {
                "delay_tolerance_years": 0.0,
                "nvda_cap_pct": 0.0,
                "rationale_md": "Zero delay tolerance forces full liquidation.",
            },
            {
                "delay_tolerance_years": 1.0,
                "nvda_cap_pct": 0.18,
                "rationale_md": "1-yr tolerance: sequence_cap binds at 18%.",
            },
            {
                "delay_tolerance_years": 2.0,
                "nvda_cap_pct": 0.30,
                "rationale_md": "2-yr tolerance: tax_liquidity_cap binds at 30%.",
            },
        ],
        "sell_down_glidepath_md": "Q1 2026: 750 shares; Q2: 750; Q3: 750; Q4: 750.",
        "advisor_intake_questions": [],
        "confidence": "HIGH",
        "cited_sources": [
            "portfolio_snapshot:NVDA",
            "sigma_calibrator.NVDA",
            "tax_analyst.effective_cgt",
        ],
    }
    out = ConcentrationAnalystOutput.model_validate(payload)
    assert out.nvda_cap_pct == 0.18
    assert len(out.constraints) == 4
    # The MIN identity is the agent's job to honour, not the schema's —
    # the schema only enforces presence of all four constraint rows.
    names = {row.name for row in out.constraints}
    assert names == {
        "sequence_cap",
        "tail_loss_cap",
        "risk_contribution_cap",
        "tax_liquidity_cap",
    }
    assert len(out.delay_sensitivities) == 3


def test_concentration_analyst_output_rejects_partial_constraints() -> None:
    """Schema rejects output with any of the four constraints missing."""
    payload = {
        "current_nvda_pct": 0.68,
        "current_risk_contribution_pct": 0.78,
        "tail_loss_p5_1y_pct": 0.27,
        "constraints": [
            # Only 3 — missing tax_liquidity_cap.
            {
                "name": "sequence_cap",
                "value_pct": 0.18,
                "derivation_md": "…",
                "confidence": "HIGH",
            },
            {
                "name": "tail_loss_cap",
                "value_pct": 0.22,
                "derivation_md": "…",
                "confidence": "HIGH",
            },
            {
                "name": "risk_contribution_cap",
                "value_pct": 0.25,
                "derivation_md": "…",
                "confidence": "MEDIUM",
            },
        ],
        "nvda_cap_pct": 0.18,
        "delay_sensitivities": [],
        "confidence": "MEDIUM",
        "cited_sources": [],
    }
    with pytest.raises(ValidationError) as exc:
        ConcentrationAnalystOutput.model_validate(payload)
    msg = str(exc.value)
    assert "tax_liquidity_cap" in msg


def test_concentration_analyst_output_rejects_duplicate_constraints() -> None:
    """Schema rejects output with duplicate constraint names."""
    payload = {
        "current_nvda_pct": 0.68,
        "current_risk_contribution_pct": 0.78,
        "tail_loss_p5_1y_pct": 0.27,
        "constraints": [
            {
                "name": "sequence_cap",
                "value_pct": 0.18,
                "derivation_md": "…",
                "confidence": "HIGH",
            },
            {
                "name": "sequence_cap",  # duplicate
                "value_pct": 0.20,
                "derivation_md": "…",
                "confidence": "HIGH",
            },
            {
                "name": "tail_loss_cap",
                "value_pct": 0.22,
                "derivation_md": "…",
                "confidence": "HIGH",
            },
            {
                "name": "risk_contribution_cap",
                "value_pct": 0.25,
                "derivation_md": "…",
                "confidence": "MEDIUM",
            },
            {
                "name": "tax_liquidity_cap",
                "value_pct": 0.35,
                "derivation_md": "…",
                "confidence": "MEDIUM",
            },
        ],
        "nvda_cap_pct": 0.18,
        "delay_sensitivities": [],
        "confidence": "MEDIUM",
        "cited_sources": [],
    }
    with pytest.raises(ValidationError) as exc:
        ConcentrationAnalystOutput.model_validate(payload)
    assert "duplicate" in str(exc.value).lower() or "sequence_cap" in str(exc.value)


def test_constraint_row_value_pct_bounded_0_to_1() -> None:
    """ConstraintRow.value_pct is bounded [0.0, 1.0]."""
    with pytest.raises(ValidationError):
        ConstraintRow(
            name="sequence_cap",
            value_pct=1.5,
            derivation_md="x",
            confidence="HIGH",
        )
    with pytest.raises(ValidationError):
        ConstraintRow(
            name="sequence_cap",
            value_pct=-0.1,
            derivation_md="x",
            confidence="HIGH",
        )


def test_delay_sensitivity_row_validates() -> None:
    """DelaySensitivityRow accepts the standard 0/1/2 year shape."""
    row = DelaySensitivityRow(
        delay_tolerance_years=1.0,
        nvda_cap_pct=0.20,
        rationale_md="sequence_cap binds at 20% under 1-yr tolerance",
    )
    assert row.nvda_cap_pct == 0.20


def test_concentration_report_extends_with_derivation_fields() -> None:
    """Legacy ConcentrationReport accepts the new derivation fields too.

    Back-compat contract: a partial report (legacy fields only) still
    validates with new fields at their defaults. A report carrying full
    derivation also validates.
    """
    # Legacy-only — back-compat path.
    legacy = ConcentrationReport(
        breaches=[Breach(category="NVDA", actual_pct=68.0, cap_pct=25.0)],
        deltas_vs_target={"NVDA": 43.0},
        summary="NVDA over cap",
    )
    assert legacy.nvda_cap_pct == 0.0
    assert legacy.constraints == []

    # Full derivation path.
    full = ConcentrationReport.model_validate({
        "breaches": [],
        "deltas_vs_target": {},
        "nvda_pace": {"shares_sold_ytd": 0, "target_shares_ytd": 0,
                       "delta_shares": 0, "on_track": True},
        "summary": "Cap derived at 18%.",
        "confidence": "HIGH",
        "cited_sources": ["portfolio_snapshot:NVDA"],
        "current_nvda_pct": 0.68,
        "current_risk_contribution_pct": 0.78,
        "tail_loss_p5_1y_pct": 0.27,
        "constraints": [
            {"name": "sequence_cap", "value_pct": 0.18,
             "derivation_md": "…", "confidence": "HIGH"},
            {"name": "tail_loss_cap", "value_pct": 0.22,
             "derivation_md": "…", "confidence": "HIGH"},
            {"name": "risk_contribution_cap", "value_pct": 0.25,
             "derivation_md": "…", "confidence": "MEDIUM"},
            {"name": "tax_liquidity_cap", "value_pct": 0.35,
             "derivation_md": "…", "confidence": "MEDIUM"},
        ],
        "nvda_cap_pct": 0.18,
        "delay_sensitivities": [
            {"delay_tolerance_years": 0.0, "nvda_cap_pct": 0.0,
             "rationale_md": "force liquidation"},
            {"delay_tolerance_years": 1.0, "nvda_cap_pct": 0.18,
             "rationale_md": "sequence binds"},
            {"delay_tolerance_years": 2.0, "nvda_cap_pct": 0.30,
             "rationale_md": "tax binds"},
        ],
        "sell_down_glidepath_md": "Q1: 750; Q2: 750; Q3: 750; Q4: 750.",
        "advisor_intake_questions": [],
    })
    assert full.nvda_cap_pct == 0.18
    assert len(full.constraints) == 4


# ---------------------------------------------------------------------------
# Synthesizer prompt — FORBIDDEN derivation rule (Codex Q9 cross-check)
# ---------------------------------------------------------------------------


def test_plan_synthesizer_prompt_forbids_derivation() -> None:
    """plan_synthesizer carries the explicit derivation-ownership rule."""
    from datetime import date as _date

    from argosy.agents.plan_synthesizer import PlanSynthesizerAgent

    agent = PlanSynthesizerAgent(user_id="ariel")
    system, _user = agent.build_prompt(
        baseline_distillate_md="(no baseline)",
        prior_current_md="",
        analyst_reports_text="",
        debate_outcomes_text="",
        portfolio_snapshot_summary="",
        recent_fills_summary="",
    )
    # The hard rule must name the FORBIDDEN posture + the canonical
    # field path the synth must READ instead of inventing.
    low = system.lower()
    assert "forbidden" in low
    assert "concentration_analyst.nvda_cap_pct" in system
    assert "WithdrawalSequencerAgent" in system
    assert "[derivation pending]" in system

