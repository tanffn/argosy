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


def test_breakdown_exclude_nvda_renormalizes_to_ex_nvda_book():
    snap = _snap([
        _pos("NVDA", "NVIDIA", 600.0),
        _pos("VOO", "Core Equity", 100.0),
        _pos("CSPX", "Core Equity", 100.0),
        _pos("SCHD", "Dividend", 100.0),
        _pos("-", "Cash", 100.0),
    ])  # total 1000k, ex-NVDA 400k
    rows = build_allocation_breakdown(snap, _doc(), exclude_nvda=True)
    by = {r.label: r for r in rows}
    # NVDA gone entirely.
    assert "Strategic single-stock (NVDA)" not in by
    # Core Equity now 200/400 = 50% of the ex-NVDA book (was 20%).
    assert round(by["US broad-market core"].current_pct, 1) == 50.0
    # Conservation holds over the ex-NVDA book.
    assert round(sum(r.current_pct for r in rows), 1) == 100.0


def test_breakdown_blank_asset_type_inherits_sibling_ticker_type():
    # The $3K Schwab SCHG row has a blank asset_type; a $17K Leumi SCHG row is
    # "Growth". Same ticker → the blank inherits Growth (US growth tilt), not
    # the "Unclassified" bucket.
    snap = _snap([
        _pos("SCHG", "", 3.0, details=""),
        _pos("SCHG", "Growth", 17.0, details="(...) SCHG"),
        _pos("NVDA", "NVIDIA", 80.0),
    ])
    rows = build_allocation_breakdown(snap, _doc())
    labels = {r.label for r in rows}
    assert "Unclassified" not in labels
    growth = next(r for r in rows if r.label == "US growth tilt (ex-NVDA)")
    assert {h.symbol for h in growth.holdings} == {"SCHG"}
    assert round(growth.current_value_k, 1) == 20.0


def test_breakdown_pure_non_us_equity_routes_to_international():
    # TA-200 (Israel) and EIMI (EM, source asset_type REIT) must NOT sit under
    # "US broad-market core" / "Real assets" — they route to International.
    snap = _snap([
        _pos('מחקה ת"א-200', "Core Equity", 40.0, details='ATF מחקה ת"א-200'),
        _pos("EIMI", "REIT", 16.0, details="(ISHR CORE EM IMI) EIMI LN"),
        _pos("CSPX", "Core Equity", 44.0, details="(ISHR CORE S&P500) CSPX LN"),
    ])
    rows = build_allocation_breakdown(snap, _doc())
    by = {r.label: r for r in rows}
    intl = by.get("International developed (ex-US)")
    assert intl is not None
    assert {h.symbol for h in intl.holdings} == {'מחקה ת"א-200', "EIMI"}
    # The genuine US ETF stays in US core.
    assert "CSPX" in {h.symbol for h in by["US broad-market core"].holdings}


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
