from datetime import date

from argosy.quality.coherence_gate import check_cap_cite_derivation
from argosy.quality.gate_types import GateCheck
from argosy.quality.plan_output_gate import gate_plan_output


def test_cap_change_with_derivation_is_clean():
    text = "the 18% cap is risk-derived from the deconcentration glide"
    assert (
        check_cap_cite_derivation(
            current_cap_pct=18.0, prior_cap_pct=13.0, plan_text=text
        )
        == []
    )


def test_cap_change_without_justification_is_flagged():
    text = "the NVDA concentration cap is now 18%."
    viol = check_cap_cite_derivation(
        current_cap_pct=18.0, prior_cap_pct=13.0, plan_text=text
    )
    assert len(viol) == 1
    assert viol[0].check is GateCheck.CAP_DERIVATION
    assert viol[0].locator == "nvda_cap"


def test_cap_unchanged_is_clean():
    assert (
        check_cap_cite_derivation(
            current_cap_pct=13.0, prior_cap_pct=13.0, plan_text="no mention"
        )
        == []
    )


def test_cap_attributed_to_user_is_flagged():
    text = "we kept your chosen 18% cap as you requested."
    viol = check_cap_cite_derivation(
        current_cap_pct=18.0, prior_cap_pct=13.0, plan_text=text
    )
    assert len(viol) >= 1
    assert any(v.check is GateCheck.CAP_DERIVATION for v in viol)


def test_no_prior_is_clean():
    assert (
        check_cap_cite_derivation(
            current_cap_pct=18.0, prior_cap_pct=None, plan_text="anything"
        )
        == []
    )


def test_no_current_is_clean():
    assert (
        check_cap_cite_derivation(
            current_cap_pct=None, prior_cap_pct=13.0, plan_text="anything"
        )
        == []
    )


# --- wiring through gate_plan_output ------------------------------------------


def test_gate_plan_output_wires_all_three_new_checks():
    """All three new checks route through gate_plan_output via their kwargs."""
    verdict = gate_plan_output(
        horizon_text={
            "medium": (
                "The NVDA cap is now 18%. "
                "USD/NIS 0.33. "
                "The retainer (2026-06-10) is on-deck."
            )
        },
        today=date(2026, 6, 16),
        fx_usd_nis=0.34,
        current_nvda_cap_pct=18.0,
        prior_nvda_cap_pct=13.0,
    )
    assert verdict.for_check(GateCheck.CAP_DERIVATION)
    assert verdict.for_check(GateCheck.FX_UNIT_DIRECTION)
    assert verdict.for_check(GateCheck.OUTPUT_DATE_STALENESS)


def test_gate_plan_output_skips_new_checks_when_inputs_absent():
    """Clean prose + no new kwargs → no new-check violations (skip discipline)."""
    verdict = gate_plan_output(horizon_text={"medium": "A clean plan paragraph."})
    assert verdict.for_check(GateCheck.CAP_DERIVATION) == []
    assert verdict.for_check(GateCheck.FX_UNIT_DIRECTION) == []
    assert verdict.for_check(GateCheck.OUTPUT_DATE_STALENESS) == []
