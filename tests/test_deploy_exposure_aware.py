"""cash_only_deploy exposure-awareness: credit held substitutes to a sleeve's
fill so the engine tops up what you hold instead of opening a duplicate ticker.

Gated behind ``exposure_aware`` (default off = the codex-verified legacy behavior).
"""
from __future__ import annotations

import datetime as dt

from argosy.services.allocation_engine import cash_only_deploy

_AS_OF = dt.date(2026, 7, 2)


def _doc(sleeves):
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc,
        AllocationInstrument,
        TargetAllocationDoc,
    )

    per = round(100.0 / len(sleeves), 2)
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


def _buys(cands):
    return {leg.symbol: leg.notional_usd for c in cands for leg in c.legs}


def test_topup_substitute_line_is_plan_bound_core_and_estate_safe():
    """A top-up buy of a held estate-safe substitute (FWRA) that fills the EXUS
    sleeve must be classified as plan-bound CORE with an estate-safe tag — not
    tactical/'medium' with an unstamped estate (codex blocker #3)."""
    from argosy.services.deployment_advisor import assemble_deployment_plan

    doc = _doc(_SLEEVES)
    plan = assemble_deployment_plan(
        doc=doc, holdings={"FWRA": 10_000.0}, deploy_amount_usd=200_000.0,
        as_of=_AS_OF, use_high_potential=False, exposure_aware=True,
    )
    fwra = [l for t in plan.tiers for l in t.lines if l.symbol == "FWRA"]
    assert fwra, "expected a FWRA top-up line"
    assert fwra[0].tier == "core"
    assert fwra[0].estate.status == "estate_safe"


def test_default_off_is_unchanged_opens_plan_tickers():
    """Without the flag, behavior is the legacy one: buys the plan's own tickers."""
    doc = _doc(_SLEEVES)
    cands = cash_only_deploy(doc, {"SCHD": 500_000.0}, 40_000.0, as_of=_AS_OF)
    syms = _buys(cands)
    # Legacy: SCHD is not credited, so FUSA (the plan dividend ticker) is bought.
    assert "FUSA" in syms


def test_full_substitute_coverage_opens_no_duplicate():
    """SCHD (~$500k) more than fills the dividend sleeve → the engine must NOT open
    FUSA; the cash flows to the genuinely-empty sleeves instead."""
    doc = _doc(_SLEEVES)
    cands = cash_only_deploy(
        doc, {"SCHD": 500_000.0}, 40_000.0, as_of=_AS_OF, exposure_aware=True,
    )
    syms = _buys(cands)
    assert "FUSA" not in syms
    # Conservation: never deploy more than the cash.
    assert sum(syms.values()) <= 40_000.0 + 0.01


def test_topup_buys_held_substitute_not_plan_ticker():
    """A partial estate-safe substitute (FWRA) for the ex-US sleeve → the incremental
    gap is deployed into FWRA (top up what you hold), not a new EXUS position."""
    doc = _doc(_SLEEVES)
    cands = cash_only_deploy(
        doc, {"FWRA": 10_000.0}, 200_000.0, as_of=_AS_OF, exposure_aware=True,
    )
    syms = _buys(cands)
    assert "FWRA" in syms
    assert "EXUS" not in syms
    assert sum(syms.values()) <= 200_000.0 + 0.01


def test_migrate_substitute_buys_plan_ticker_for_incremental_gap():
    """A US-domiciled substitute (SCHD) that only PARTIALLY fills the sleeve → the
    incremental gap is bought in the plan's estate-safe FUSA (migration start), NOT
    added to SCHD, and the leg is marked as a migration."""
    doc = _doc(_SLEEVES)
    # SCHD small vs a large deploy so an incremental dividend-sleeve gap remains.
    cands = cash_only_deploy(
        doc, {"SCHD": 5_000.0}, 400_000.0, as_of=_AS_OF, exposure_aware=True,
    )
    syms = _buys(cands)
    assert "FUSA" in syms  # estate-safe target for the incremental gap
    assert "SCHD" not in syms  # never add fresh cash to the US-situs holding
    fusa_cand = next(c for c in cands for leg in c.legs if leg.symbol == "FUSA")
    assert any("migrat" in x.lower() for x in fusa_cand.cites) or "migrat" in fusa_cand.rationale.lower()
