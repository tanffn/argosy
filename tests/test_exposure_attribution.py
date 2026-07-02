"""Exposure attribution — credit held instruments to the plan sleeve they cover,
so deployment stops opening a new ticker when you already hold the exposure.

Classification authority is instrument_reference (asset_class / sector / region /
structure / estate_safe), NOT raw snapshot categories. Attribution is separate from
implementation equivalence: a held US-domiciled substitute (SCHD, O) CREDITS the
sleeve fill but is flagged for MIGRATION (the plan's UCITS instrument is estate-safe),
while a held estate-safe ETF of the same exposure is a TOP-UP (don't open the plan
ticker at all)."""
from __future__ import annotations

from argosy.services.exposure_attribution import classify_plan_substitutes


def _doc(sleeves):
    """sleeves: {label: plan_instrument_ticker}."""
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc,
        AllocationInstrument,
        TargetAllocationDoc,
    )

    per = round(100.0 / max(1, len(sleeves)), 2)
    classes = [
        AllocationClassDoc(
            label=label, snapshot_category=label, sigma_class="us_equity",
            target_pct=per,
            instruments=[AllocationInstrument(symbol=tk, role="primary",
                                              weight_within_class_pct=100.0,
                                              rationale="", domicile=None)],
            agreement="", rationale="", dissent="",
        )
        for label, tk in sleeves.items()
    ]
    return TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.16, nvda_cap_pct=13.0, fi_pct=10.0,
        provenance="test", classes=classes, glide=[],
    )


_SLEEVES = {
    "Dividend-quality income": "FUSA",
    "International developed (ex-US)": "EXUS",
    "Real assets (REIT/TIPS)": "DPYA",
    "US low-volatility equity": "SPMV",
}


def _by_held(subs):
    return {s.held_ticker: s for s in subs}


def test_us_domiciled_dividend_substitute_is_migrate():
    """SCHD (US-domiciled dividend ETF) covers the FUSA dividend sleeve → credited,
    but flagged migrate (FUSA is the estate-safe implementation)."""
    subs = classify_plan_substitutes(_doc(_SLEEVES), {"SCHD": 264_000.0})
    by = _by_held(subs)
    assert "SCHD" in by
    assert by["SCHD"].plan_instrument == "FUSA"
    assert by["SCHD"].disposition == "migrate"


def test_estate_safe_exus_substitute_is_topup():
    """FWRA (estate-safe global broad-index ETF) covers the EXUS sleeve → top up the
    held fund, do NOT open EXUS."""
    subs = classify_plan_substitutes(_doc(_SLEEVES), {"FWRA": 93_000.0})
    by = _by_held(subs)
    assert "FWRA" in by
    assert by["FWRA"].plan_instrument == "EXUS"
    assert by["FWRA"].disposition == "topup"


def test_us_single_reit_is_migrate_but_estate_safe_property_etf_is_topup():
    subs = classify_plan_substitutes(_doc(_SLEEVES), {"O": 19_000.0, "IWDP": 34_000.0})
    by = _by_held(subs)
    assert by["O"].plan_instrument == "DPYA" and by["O"].disposition == "migrate"
    assert by["IWDP"].plan_instrument == "DPYA" and by["IWDP"].disposition == "topup"


def test_wrong_factor_is_not_attributed():
    """VTV (value) / SPMO (momentum) are NOT the low-vol sleeve — factor sleeves do
    not collide, so SPMV stays a genuine new open."""
    subs = classify_plan_substitutes(_doc(_SLEEVES), {"VTV": 33_000.0, "SPMO": 23_000.0})
    assert not any(s.plan_instrument == "SPMV" for s in subs)


def test_us_broad_index_not_credited_to_exus_global_sleeve():
    """CSPX (US broad index) must NOT be credited to the ex-US/global sleeve — region
    matters for equity, or we double-count US already held as core."""
    subs = classify_plan_substitutes(_doc(_SLEEVES), {"CSPX": 156_000.0})
    assert not any(s.plan_instrument == "EXUS" for s in subs)


def test_holding_equal_to_plan_ticker_is_not_a_substitute():
    """If you already hold the plan's exact instrument, that's a normal top-up the
    deploy engine handles — not a substitute."""
    subs = classify_plan_substitutes(_doc(_SLEEVES), {"FUSA": 10_000.0})
    assert not subs
