"""Live current-allocation vs canonical plan-target, by class, with per-symbol
drill-down (the /portfolio 'Allocation vs target' rework)."""
from __future__ import annotations

from types import SimpleNamespace

from argosy.services.allocation_breakdown import build_allocation_breakdown
from argosy.services.target_allocation_doc import OTHER_SINGLES_LABEL


def _re_pos(usd_k):
    return SimpleNamespace(symbol="-", asset_type="Real estate", usd_value_k=usd_k,
                           details="Real estate", location="Aborad", currency="USD")


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
    growth = next(r for r in rows if r.label == "Global quality growth (ex-NVDA-dense)")
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


def test_breakdown_sgov_counts_in_cash_and_tbills():
    # SGOV's raw asset_type is "Defensive", but it's a T-bill ETF → the
    # reference classifies it Cash, so it belongs in "Cash & T-bills", not
    # "Short-duration IG bonds".
    snap = _snap([
        _pos("SGOV", "Defensive", 105.0, details="(...) SGOV"),
        _pos("-", "Cash", 20.0, details="Cash"),
        _pos("NVDA", "NVIDIA", 75.0),
    ])
    rows = build_allocation_breakdown(snap, _doc())
    by = {r.label: r for r in rows}
    cash = by.get("Cash & T-bills (incl. ILS tranche)")
    assert cash is not None
    assert {h.symbol for h in cash.holdings} >= {"SGOV"}
    # SGOV ($105) + cash ($20) both land here.
    assert round(cash.current_value_k, 1) == 125.0
    assert "Short-duration IG bonds" not in by


def test_targets_sum_to_100_including_unheld_plan_classes():
    # Hold only 2 of the 4 plan classes; the unheld ones must still appear as
    # 0%-current rows so the target column conserves to 100%.
    snap = _snap([_pos("NVDA", "NVIDIA", 130.0), _pos("VOO", "Core Equity", 870.0)])
    rows = build_allocation_breakdown(snap, _doc())
    by = {r.label: r for r in rows}
    assert by["Dividend-quality income"].current_pct == 0.0
    assert by["Cash & T-bills (incl. ILS tranche)"].current_pct == 0.0
    assert round(sum(r.target_pct or 0.0 for r in rows), 1) == 100.0


def test_physical_real_estate_excluded_from_investable_breakdown():
    snap = _snap([_pos("VOO", "Core Equity", 100.0), _re_pos(69.0)])
    rows = build_allocation_breakdown(snap, _doc())
    # The $69K physical property is gone; the book total is just VOO.
    assert all(h.value_k != 69.0 for r in rows for h in r.holdings)
    voo = next(r for r in rows if r.label == "US broad-market core")
    assert voo.current_pct == 100.0


def test_redeploy_singles_show_zero_target_not_none():
    snap = _snap([_pos("SOFI", "Individual Stocks", 50.0),
                  _pos("VOO", "Core Equity", 50.0)])
    rows = build_allocation_breakdown(snap, _doc())
    singles = next(r for r in rows if r.label == OTHER_SINGLES_LABEL)
    assert singles.target_pct == 0.0


def test_bare_equity_maps_to_core_not_orphan():
    snap = _snap([_pos("BRK/B", "Equity", 90.0), _pos("VOO", "Core Equity", 10.0)])
    rows = build_allocation_breakdown(snap, _doc())
    by = {r.label: r for r in rows}
    assert "Equity" not in by, "no orphan 'Equity' catch-all row"
    assert "BRK/B" in {h.symbol for h in by["US broad-market core"].holdings}


def test_exclude_nvda_renormalises_targets_to_100():
    snap = _snap([_pos("NVDA", "NVIDIA", 600.0), _pos("VOO", "Core Equity", 400.0)])
    rows = build_allocation_breakdown(snap, _doc(), exclude_nvda=True)
    assert "Strategic single-stock (NVDA)" not in {r.label for r in rows}
    assert round(sum(r.target_pct or 0.0 for r in rows), 0) == 100.0


def test_exclude_nvda_does_not_zero_ex_nvda_labelled_classes():
    # The renormalization must drop ONLY the NVDA strategic class — not any
    # class whose label contains the "NVDA" substring (e.g. "US growth tilt
    # (ex-NVDA)"), which would wrongly zero its target.
    from datetime import date
    from argosy.services.target_allocation_doc import (
        TargetAllocationDoc, AllocationClassDoc, AllocationInstrument, GlideWaypoint,
    )

    def cls(label, sym, pct):
        return AllocationClassDoc(label=label, snapshot_category=label,
            sigma_class="x", target_pct=pct,
            instruments=[AllocationInstrument(symbol=sym, role="primary",
                weight_within_class_pct=100.0, domicile="IE")])

    doc = TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="t",
        classes=[cls("Strategic single-stock (NVDA)", "NVDA", 20.0),
                 cls("US growth tilt (ex-NVDA)", "QQQM", 16.0),
                 cls("US broad-market core", "CSPX", 64.0)],
        glide=[GlideWaypoint(quarter=0, date=date(2026, 1, 1),
               composition_pct_by_class={})],
    )
    snap = _snap([_pos("NVDA", "NVIDIA", 600.0), _pos("QQQM", "Growth", 200.0),
                  _pos("CSPX", "Core Equity", 200.0)])
    rows = build_allocation_breakdown(snap, doc, exclude_nvda=True)
    by = {r.label: r for r in rows}
    assert "Strategic single-stock (NVDA)" not in by
    # The doc above deliberately keeps the LEGACY sleeve label — the breakdown
    # normalizes doc labels through the alias map, so the row keys the CURRENT one.
    assert (by["Global quality growth (ex-NVDA-dense)"].target_pct or 0) > 0  # NOT zeroed
    assert round(sum(r.target_pct or 0.0 for r in rows), 0) == 100.0


def _doc_with_moonshot():
    """The live plan-v67 shape: a High-growth / high-potential class whose
    instruments are named single stocks (the x10 moonshot sleeve)."""
    from datetime import date
    from argosy.services.target_allocation_doc import (
        TargetAllocationDoc, AllocationClassDoc, AllocationInstrument, GlideWaypoint,
    )

    def cls(label, syms, pct):
        n = len(syms)
        return AllocationClassDoc(label=label, snapshot_category=label,
            sigma_class="x", target_pct=pct,
            instruments=[AllocationInstrument(symbol=s, role="primary",
                weight_within_class_pct=round(100.0 / n, 2), domicile="US")
                for s in syms])

    return TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="t",
        classes=[cls("US broad-market core", ["CSPX"], 55.0),
                 cls("Strategic single-stock (NVDA)", ["NVDA"], 13.0),
                 cls("High-growth / high-potential",
                     ["RXRX", "ACHR", "RGTI", "OKLO", "TEM", "IONQ", "ASTS", "INVZ"],
                     5.0),
                 cls("Cash & T-bills (incl. ILS tranche)", ["IB01"], 27.0)],
        glide=[GlideWaypoint(quarter=0, date=date(2026, 1, 1),
               composition_pct_by_class={})],
    )


def test_plan_instrument_attributes_to_its_plan_class_not_asset_type():
    # Exposure-aware attribution: RXRX/OKLO/TEM arrive from the broker tagged
    # "Individual Stocks", but they ARE plan v67's moonshot-sleeve instruments
    # — they attribute to "High-growth / high-potential", never the residual
    # redeploy bucket.
    snap = _snap([
        _pos("NVDA", "NVIDIA", 800.0),
        _pos("CSPX", "Core Equity", 168.0),
        _pos("RXRX", "Individual Stocks", 6.0),
        _pos("OKLO", "Individual Stocks", 5.0),
        _pos("TEM", "Individual Stocks", 5.0),
        _pos("GOOG", "Individual Stocks", 16.0),  # true off-plan legacy single
    ])
    rows = build_allocation_breakdown(snap, _doc_with_moonshot())
    by = {r.label: r for r in rows}
    moonshot = by["High-growth / high-potential"]
    assert {h.symbol for h in moonshot.holdings} == {"RXRX", "OKLO", "TEM"}
    assert round(moonshot.current_value_k, 1) == 16.0
    assert moonshot.target_pct == 5.0
    # Only the true legacy single remains in the residual redeploy bucket.
    singles = by[OTHER_SINGLES_LABEL]
    assert {h.symbol for h in singles.holdings} == {"GOOG"}
    assert round(sum(r.current_pct for r in rows), 1) == 100.0


def test_legacy_single_stays_in_residual_redeploy_bucket():
    # A single stock the plan does NOT name stays in the 0%-target residual —
    # the attribution override only fires for plan-listed instruments.
    snap = _snap([
        _pos("SOFI", "Individual Stocks", 40.0),
        _pos("META", "Individual Stocks", 30.0),
        _pos("CSPX", "Core Equity", 30.0),
    ])
    rows = build_allocation_breakdown(snap, _doc_with_moonshot())
    by = {r.label: r for r in rows}
    singles = by[OTHER_SINGLES_LABEL]
    assert {h.symbol for h in singles.holdings} == {"SOFI", "META"}
    assert singles.target_pct == 0.0
    assert "High-growth / high-potential" in by  # surfaced as unheld plan class
    assert by["High-growth / high-potential"].current_pct == 0.0


def test_live_shape_moonshot_buys_out_of_redeploy_residual_ex_nvda():
    # Pin the live 2026-07-07 shape (greeting on_plan_note path): ex-NVDA
    # renormalized view, moonshot buys attribute to their sleeve, the residual
    # gap reflects legacy singles ONLY.
    snap = _snap([
        _pos("NVDA", "NVIDIA", 600.0),
        _pos("CSPX", "Core Equity", 200.0),
        _pos("RXRX", "Individual Stocks", 6.0),
        _pos("OKLO", "Individual Stocks", 5.0),
        _pos("TEM", "Individual Stocks", 5.0),
        _pos("GOOG", "Individual Stocks", 64.0),
        _pos("AMD", "Individual Stocks", 55.0),
        _pos("-", "Cash", 65.0),
    ])  # ex-NVDA book = 400
    rows = build_allocation_breakdown(snap, _doc_with_moonshot(), exclude_nvda=True)
    by = {r.label: r for r in rows}
    moonshot = by["High-growth / high-potential"]
    assert {h.symbol for h in moonshot.holdings} == {"RXRX", "OKLO", "TEM"}
    assert round(moonshot.current_pct, 1) == 4.0  # 16/400
    singles = by[OTHER_SINGLES_LABEL]
    assert {h.symbol for h in singles.holdings} == {"GOOG", "AMD"}
    assert round(singles.current_pct, 2) == 29.75  # 119/400 — the TRUE redeploy number
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
