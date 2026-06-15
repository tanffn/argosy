from types import SimpleNamespace

from argosy.quality.coherence_gate import check_cross_surface_coherence
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
