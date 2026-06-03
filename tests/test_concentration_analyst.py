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
    """ConstraintRow.value_pct is bounded [0.0, 1.0] after normalization.

    A value in (1.0, 100] is read as a percentage and scaled down (so
    1.5 -> 0.015, a valid 1.5% cap). Genuinely out-of-range values —
    above 100 or negative — still fail the bound check loudly; we never
    clamp or fabricate.
    """
    # 1.5 is now interpreted as 1.5% -> 0.015 (valid), NOT rejected.
    row = ConstraintRow(
        name="sequence_cap", value_pct=1.5, derivation_md="x", confidence="HIGH"
    )
    assert row.value_pct == 0.015
    # > 100 is unambiguously out of range -> still rejected.
    with pytest.raises(ValidationError):
        ConstraintRow(
            name="sequence_cap",
            value_pct=150.0,
            derivation_md="x",
            confidence="HIGH",
        )
    # Negative -> rejected.
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
# drun-73 regression — schema must match the model's real output shape and
# must NOT fabricate per-row data to limp past validation.
# ---------------------------------------------------------------------------


def test_concentration_report_accepts_real_llm_output_shape() -> None:
    """The exact shape live runs (drun 73) emitted now validates.

    Two things the live model did that earlier broke validation:
      1. ``deltas_vs_target`` came as a LIST of
         ``{category, actual_pct, delta_pp}`` rows, not the canonical
         ``{category: delta_pp}`` mapping.
      2. Every constraint row carried ``derivation_md`` + ``confidence``
         and every delay row carried ``nvda_cap_pct`` + ``rationale_md``
         (the model DOES emit them when prompted).

    The list form is coerced to a by-category dict — including a 0.0
    on-target row (must not be dropped) — and the full payload validates.
    """
    payload = {
        "breaches": [],
        "deltas_vs_target": [
            {"category": "NVDA", "actual_pct": 65.0, "target_pct": 15.14,
             "delta_pp": 49.86},
            # On-target row: delta_pp == 0.0 must survive coercion.
            {"category": "Cash", "actual_pct": 5.0, "delta_pp": 0.0},
        ],
        "summary": "NVDA 65% vs an 18% derived cap; sell-down required.",
        "confidence": "HIGH",
        "cited_sources": ["portfolio/holdings", "sigma_calibrator.NVDA"],
        "current_nvda_pct": 0.65,
        "current_risk_contribution_pct": 0.81,
        "tail_loss_p5_1y_pct": 0.29,
        "constraints": [
            {"name": "sequence_cap", "value_pct": 0.18,
             "derivation_md": "p5 NVDA shock pushes FI 2031->2032 at 18%.",
             "confidence": "HIGH"},
            {"name": "tail_loss_cap", "value_pct": 0.22,
             "derivation_md": "p5 portfolio loss hits 25% DD at 22%.",
             "confidence": "MEDIUM"},
            {"name": "risk_contribution_cap", "value_pct": 0.25,
             "derivation_md": "MVC hits 30% single-name limit at 25%.",
             "confidence": "MEDIUM"},
            {"name": "tax_liquidity_cap", "value_pct": 0.35,
             "derivation_md": "Section-102 windows cap net sell-down ~35%.",
             "confidence": "LOW"},
        ],
        "nvda_cap_pct": 0.18,
        "delay_sensitivities": [
            {"delay_tolerance_years": 0.0, "nvda_cap_pct": 0.0,
             "rationale_md": "Zero tolerance -> force-liquidate."},
            {"delay_tolerance_years": 1.0, "nvda_cap_pct": 0.18,
             "rationale_md": "sequence_cap binds at 18%."},
            {"delay_tolerance_years": 2.0, "nvda_cap_pct": 0.25,
             "rationale_md": "risk_contribution_cap binds at 25%."},
        ],
        "sell_down_glidepath_md": "Q1-Q4 2026: 750 sh/quarter.",
        "advisor_intake_questions": [],
    }
    out = ConcentrationReport.model_validate(payload)
    # List -> by-category dict, on-target row kept.
    assert out.deltas_vs_target == {"NVDA": 49.86, "Cash": 0.0}
    assert len(out.constraints) == 4
    assert out.constraints[0].confidence == "HIGH"
    assert out.constraints[3].confidence == "LOW"
    assert len(out.delay_sensitivities) == 3
    assert out.delay_sensitivities[1].nvda_cap_pct == 0.18


def test_concentration_report_rejects_fabricated_row_defaults() -> None:
    """A present row MUST carry its required derivation fields.

    HARD GUARDRAIL: we never default ``derivation_md`` / ``confidence``
    (constraints) or ``nvda_cap_pct`` / ``rationale_md`` (delay rows).
    Fabricating a MEDIUM confidence or a 0% cap the analyst never derived
    is strictly worse than a loud validation failure, so a row missing
    those keys must raise — exactly the four drun-73 errors, now caught
    deterministically instead of silently filled.
    """
    payload = {
        "deltas_vs_target": [
            {"category": "NVDA", "actual_pct": 65.0, "delta_pp": 49.86}
        ],
        "constraints": [
            # Missing derivation_md + confidence.
            {"name": "sequence_cap", "value_pct": 0.18},
        ],
        "nvda_cap_pct": 0.18,
        "delay_sensitivities": [
            # Missing nvda_cap_pct + rationale_md.
            {"delay_tolerance_years": 0.0},
        ],
        "cited_sources": ["portfolio/holdings"],
    }
    with pytest.raises(ValidationError) as exc:
        ConcentrationReport.model_validate(payload)
    missing = {
        tuple(e["loc"]) for e in exc.value.errors() if e["type"] == "missing"
    }
    assert ("constraints", 0, "derivation_md") in missing
    assert ("constraints", 0, "confidence") in missing
    assert ("delay_sensitivities", 0, "nvda_cap_pct") in missing
    assert ("delay_sensitivities", 0, "rationale_md") in missing


def test_concentration_report_breach_name_alias_and_optional_pcts() -> None:
    """Legacy ``breaches`` rows tolerate the model's real shape.

    Live runs key the breach by ``name`` (not ``category``) and
    sometimes omit ``actual_pct`` / ``cap_pct``. The report maps
    ``name`` -> ``category`` and leaves missing percentages as ``None``
    (never fabricated to 0) so a legacy display row can't sink the whole
    money-math report.
    """
    out = ConcentrationReport.model_validate({
        "breaches": [
            # name instead of category; cap_pct omitted.
            {"name": "NVDA", "actual_pct": 65.0, "severity": "breach",
             "note": "over the derived cap"},
        ],
        "deltas_vs_target": {"NVDA": 47.0},
        "cited_sources": ["portfolio/holdings"],
    })
    assert len(out.breaches) == 1
    assert out.breaches[0].category == "NVDA"
    assert out.breaches[0].actual_pct == 65.0
    # Omitted cap_pct stays None — NOT fabricated to 0.0.
    assert out.breaches[0].cap_pct is None


def test_concentration_report_normalizes_percentage_fraction_fields() -> None:
    """Fraction-domain fields accept the model's 0–100 percentage form.

    Live runs emit ``current_nvda_pct`` etc. as percentages (67.08)
    where the schema wants a fraction (0.6708). A value in (1.0, 100]
    is scaled down — a representation rename of a REAL value, never a
    fabricated default. The MIN-over-constraints relationship is
    preserved because cap + value_pct scale together.
    """
    out = ConcentrationReport.model_validate({
        "current_nvda_pct": 67.08,
        "current_risk_contribution_pct": 81.0,
        "tail_loss_p5_1y_pct": 29.0,
        "constraints": [
            {"name": "sequence_cap", "value_pct": 18.0,
             "derivation_md": "x", "confidence": "HIGH"},
            {"name": "tail_loss_cap", "value_pct": 22.0,
             "derivation_md": "x", "confidence": "MEDIUM"},
            {"name": "risk_contribution_cap", "value_pct": 25.0,
             "derivation_md": "x", "confidence": "MEDIUM"},
            {"name": "tax_liquidity_cap", "value_pct": 35.0,
             "derivation_md": "x", "confidence": "LOW"},
        ],
        "nvda_cap_pct": 18.0,
        "delay_sensitivities": [
            {"delay_tolerance_years": 1.0, "nvda_cap_pct": 18.0,
             "rationale_md": "x"},
        ],
        "cited_sources": ["portfolio/holdings"],
    })
    assert out.current_nvda_pct == 0.6708
    assert out.nvda_cap_pct == 0.18
    assert out.constraints[0].value_pct == 0.18
    assert out.delay_sensitivities[0].nvda_cap_pct == 0.18

    # A value already in [0,1] passes through untouched.
    frac = ConcentrationReport.model_validate(
        {"current_nvda_pct": 0.65, "cited_sources": ["x"]}
    )
    assert frac.current_nvda_pct == 0.65

    # The output-model variant normalizes too AND keeps the MIN identity.
    out2 = ConcentrationAnalystOutput.model_validate({
        "current_nvda_pct": 67.0,
        "current_risk_contribution_pct": 81.0,
        "tail_loss_p5_1y_pct": 29.0,
        "constraints": [
            {"name": "sequence_cap", "value_pct": 18.0,
             "derivation_md": "x", "confidence": "HIGH"},
            {"name": "tail_loss_cap", "value_pct": 22.0,
             "derivation_md": "x", "confidence": "MEDIUM"},
            {"name": "risk_contribution_cap", "value_pct": 25.0,
             "derivation_md": "x", "confidence": "MEDIUM"},
            {"name": "tax_liquidity_cap", "value_pct": 35.0,
             "derivation_md": "x", "confidence": "LOW"},
        ],
        "nvda_cap_pct": 18.0,  # == MIN(value_pct) after both scale by 1/100
        "delay_sensitivities": [],
        "cited_sources": ["x"],
    })
    assert out2.nvda_cap_pct == 0.18


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

