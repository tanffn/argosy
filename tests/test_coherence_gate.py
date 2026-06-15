from types import SimpleNamespace

from argosy.quality.coherence_gate import (
    check_cross_surface_coherence,
    check_fi_sufficiency_under_shock,
)
from argosy.quality.gate_types import GateCheck


def _art(surface_values):
    return SimpleNamespace(full_text="", surface_values=surface_values)


def test_coherence_flags_divergent_nvda_weight_across_surfaces():
    """Body 62.5% vs dashboard 56.9% for the same concept must fail."""
    art = _art({"nvda_weight_pct": [("body", 62.5), ("dashboard", 56.9)]})
    viol = check_cross_surface_coherence(art)
    assert len(viol) == 1
    assert viol[0].check is GateCheck.CROSS_SURFACE_COHERENCE
    assert "nvda_weight_pct" in viol[0].detail


def test_coherence_passes_when_surfaces_agree():
    art = _art({"nvda_weight_pct": [("body", 62.52), ("dashboard", 62.5)]})  # within tol
    assert check_cross_surface_coherence(art) == []


def test_coherence_flags_sign_flip_on_fi_margin():
    """The L72/L188 class: +118,020 on one surface, -118,020 on another."""
    art = _art({"fi_margin_signed_nis": [("capital_sufficiency", 118020.0), ("body", -118020.0)]})
    viol = check_cross_surface_coherence(art)
    assert len(viol) == 1 and "fi_margin_signed_nis" in viol[0].detail


# --- Task 4: FI sufficiency under NVDA shock ----------------------------------

# −30% NVDA shock drops NW below the perpetuity base (the 2026-06-15 reality).
_SHOCK_BREAKS = {
    "base": {"net_worth_nis": 11_954_153, "perpetuity_reached": True, "total_reached": True},
    "shock_0.30": {"net_worth_nis": 9_912_041, "perpetuity_reached": False, "total_reached": False},
    "shock_0.50": {"net_worth_nis": 8_550_633, "perpetuity_reached": False, "total_reached": False},
}
# A book where the −30% shock still clears the perpetuity base.
_SHOCK_SURVIVES = {
    "base": {"net_worth_nis": 20_000_000, "perpetuity_reached": True, "total_reached": True},
    "shock_0.30": {"net_worth_nis": 17_957_960, "perpetuity_reached": True, "total_reached": True},
}


def test_fi_shock_flags_unqualified_reached_claim_broken_by_tail():
    text = "Full capital sufficiency = perpetuity + reserve — capital sufficiency reached."
    viol = check_fi_sufficiency_under_shock(shock_result=_SHOCK_BREAKS, plan_text=text)
    assert len(viol) == 1
    assert viol[0].check is GateCheck.FI_SHOCK_SUFFICIENCY
    assert "perpetuity" in viol[0].detail


def test_fi_shock_passes_when_claim_is_qualified():
    text = (
        "Capital sufficiency reached, but only at the full NVDA mark: a −30% NVDA "
        "shock drops net worth below the perpetuity base."
    )
    assert check_fi_sufficiency_under_shock(shock_result=_SHOCK_BREAKS, plan_text=text) == []


def test_fi_shock_passes_when_shock_survives():
    text = "Capital sufficiency reached — perpetuity and full target both clear."
    assert check_fi_sufficiency_under_shock(shock_result=_SHOCK_SURVIVES, plan_text=text) == []


def test_shock_gate_fires_despite_unrelated_nvda_section():
    """An incidental NVDA/drawdown mention in a *different* sentence must NOT
    suppress the violation (the old document-global qualifier match did)."""
    text = (
        "Capital sufficiency reached. Separately, NVDA had a strong drawdown "
        "last quarter but recovered."
    )
    viol = check_fi_sufficiency_under_shock(shock_result=_SHOCK_BREAKS, plan_text=text)
    assert len(viol) == 1
    assert viol[0].check is GateCheck.FI_SHOCK_SUFFICIENCY


def test_shock_gate_catches_financially_independent_phrasing():
    text = "You have reached financial independence at the current mark."
    viol = check_fi_sufficiency_under_shock(shock_result=_SHOCK_BREAKS, plan_text=text)
    assert len(viol) == 1


def test_shock_gate_does_not_flag_negated_claim():
    text = "Capital sufficiency is not yet reached."
    assert check_fi_sufficiency_under_shock(shock_result=_SHOCK_BREAKS, plan_text=text) == []


def test_shock_gate_does_not_flag_same_sentence_qualifier():
    text = (
        "Capital sufficiency is reached at the full NVDA mark, but a 30% NVDA "
        "drawdown breaks the perpetuity base."
    )
    assert check_fi_sufficiency_under_shock(shock_result=_SHOCK_BREAKS, plan_text=text) == []
