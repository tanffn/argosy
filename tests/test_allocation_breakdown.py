"""Live current-allocation vs canonical plan-target, by class, with per-symbol
drill-down (the /portfolio 'Allocation vs target' rework)."""
from __future__ import annotations

from types import SimpleNamespace

from argosy.services.allocation_breakdown import build_allocation_breakdown


def _pos(symbol, asset_type, usd_k, details=""):
    return SimpleNamespace(symbol=symbol, asset_type=asset_type,
                           usd_value_k=usd_k, details=details)


def _snap(positions):
    return SimpleNamespace(positions=positions)


def _doc():
    from argosy.services.target_allocation_doc import (
        TargetAllocationDoc, AllocationClassDoc, AllocationInstrument, GlideWaypoint,
    )
    from datetime import date

    def cls(label, sym, pct):
        return AllocationClassDoc(label=label, snapshot_category=label,
            sigma_class="x", target_pct=pct,
            instruments=[AllocationInstrument(symbol=sym, role="primary",
                weight_within_class_pct=100.0, domicile="IE")])
    return TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="t",
        classes=[cls("US broad-market core", "CSPX", 40.0),
                 cls("Strategic single-stock (NVDA)", "NVDA", 13.0),
                 cls("Dividend-quality income", "FUSA", 12.0),
                 cls("Cash & T-bills (incl. ILS tranche)", "IB01", 35.0)],
        glide=[GlideWaypoint(quarter=0, date=date(2026, 1, 1),
               composition_pct_by_class={"US broad-market core": 40.0})],
    )


def test_breakdown_live_current_vs_target_with_holdings():
    snap = _snap([
        _pos("NVDA", "NVIDIA", 600.0),
        _pos("VOO", "Core Equity", 100.0),
        _pos("CSPX", "Core Equity", 100.0),
        _pos("SCHD", "Dividend", 100.0),
        _pos("-", "Cash", 100.0),
    ])  # total 1000k
    rows = build_allocation_breakdown(snap, _doc())
    by = {r.label: r for r in rows}

    # current % is from LIVE holdings (NVDA 60% — the real concentration)
    assert round(by["Strategic single-stock (NVDA)"].current_pct, 1) == 60.0
    assert round(by["Strategic single-stock (NVDA)"].target_pct, 1) == 13.0
    # Core Equity = VOO+CSPX = 200/1000 = 20% current vs 40% target
    core = by["US broad-market core"]
    assert round(core.current_pct, 1) == 20.0
    assert round(core.target_pct, 1) == 40.0
    # drill-down: the symbols that fell into Core Equity, with value + %
    syms = sorted((h.symbol, round(h.value_k, 0), round(h.pct, 1)) for h in core.holdings)
    assert syms == [("CSPX", 100.0, 10.0), ("VOO", 100.0, 10.0)]
    # conservation: current %s sum to ~100
    assert round(sum(r.current_pct for r in rows), 1) == 100.0


def test_breakdown_unmapped_category_surfaces_with_zero_target():
    snap = _snap([_pos("NVDA", "NVIDIA", 500.0),
                  _pos("WEIRD", "Crypto-thing", 500.0)])
    rows = build_allocation_breakdown(snap, _doc())
    by = {r.label: r for r in rows}
    # the unmapped category is surfaced (not dropped) with no plan target
    other = [r for r in rows if r.label not in (
        "Strategic single-stock (NVDA)", "US broad-market core",
        "Dividend-quality income", "Cash & T-bills (incl. ILS tranche)")]
    assert other and any(h.symbol == "WEIRD" for r in other for h in r.holdings)
    assert round(sum(r.current_pct for r in rows), 1) == 100.0
