"""Tests for the deterministic allocation engine (pure; no network/DB)."""
from __future__ import annotations

from argosy.services.allocation_engine import (
    AllocationCandidate,
    AllocationLeg,
    AllocationMode,
    REPLACES_SYMBOLS,
)


def test_value_objects_and_replacement_map():
    leg = AllocationLeg(side="BUY", symbol="CSPX", account_id="ibkr",
                        currency="USD", notional_usd=1000.0,
                        funding_source="cash")
    cand = AllocationCandidate(kind="BUY", legs=(leg,), horizon="now")
    assert cand.legs[0].symbol == "CSPX"
    assert cand.total_notional_usd == 1000.0
    # documented UCITS swaps are present
    assert REPLACES_SYMBOLS["SCHD"] == "FUSA"
    assert REPLACES_SYMBOLS["VOO"] == "CSPX"
    assert AllocationMode.CASH_ONLY_DEPLOY.value == "cash_only_deploy"


def _doc(glide_dates_pct, class_final):
    """Build a TargetAllocationDoc with a glide and final class targets."""
    from argosy.services.target_allocation_doc import (
        TargetAllocationDoc, AllocationClassDoc, AllocationInstrument, GlideWaypoint,
    )
    classes = [
        AllocationClassDoc(
            label=lbl, snapshot_category=lbl, sigma_class="us_equity",
            target_pct=pct,
            instruments=[AllocationInstrument(symbol=sym, role="primary",
                                              weight_within_class_pct=100.0, domicile="IE")],
        )
        for lbl, pct, sym in class_final
    ]
    glide = [GlideWaypoint(quarter=i, date=d, composition_pct_by_class=comp)
             for i, (d, comp) in enumerate(glide_dates_pct)]
    return TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="test", classes=classes, glide=glide,
    )


def test_class_targets_as_of_picks_latest_waypoint_on_or_before():
    from datetime import date
    from argosy.services.allocation_engine import class_targets_as_of
    doc = _doc(
        glide_dates_pct=[
            (date(2026, 3, 31), {"Core": 60.0, "Bonds": 40.0}),
            (date(2026, 9, 30), {"Core": 70.0, "Bonds": 30.0}),
        ],
        class_final=[("Core", 70.0, "CSPX"), ("Bonds", 30.0, "IB01")],
    )
    # as_of between the two waypoints -> the earlier (current) one, NOT the end-state
    assert class_targets_as_of(doc, date(2026, 6, 1)) == {"Core": 60.0, "Bonds": 40.0}
    # as_of after the last waypoint -> the last
    assert class_targets_as_of(doc, date(2026, 12, 1)) == {"Core": 70.0, "Bonds": 30.0}


def test_class_targets_as_of_falls_back_to_final_when_no_glide():
    from datetime import date
    from argosy.services.allocation_engine import class_targets_as_of
    doc = _doc(glide_dates_pct=[], class_final=[("Core", 65.0, "CSPX"), ("Bonds", 35.0, "IB01")])
    assert class_targets_as_of(doc, date(2026, 6, 1)) == {"Core": 65.0, "Bonds": 35.0}


def test_target_values_by_symbol_uses_glide_pct_and_instrument_weights():
    from datetime import date
    from argosy.services.allocation_engine import target_values_by_symbol
    doc = _doc(
        glide_dates_pct=[(date(2026, 3, 31), {"Core": 60.0, "Bonds": 40.0})],
        class_final=[("Core", 70.0, "CSPX"), ("Bonds", 30.0, "IB01")],
    )
    # book = 1000; glide Core=60% -> CSPX 600, Bonds=40% -> IB01 400
    out = target_values_by_symbol(doc, total=1000.0, as_of=date(2026, 6, 1))
    assert out["CSPX"] == 600.0
    assert out["IB01"] == 400.0


def test_target_values_conservation_surfaces_unmapped_label():
    """codex #1: a glide label with no matching instrument class (a transitional
    'redeploy' band) is surfaced as an explicit unmapped bucket, never silently
    dropped — so Σ target values still equals the book total."""
    from datetime import date
    from argosy.services.allocation_engine import target_values_by_symbol, UNMAPPED_BUCKET
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1),
                          {"Core": 70.0,
                           "Individual Stocks (non-NVDA, to redeploy)": 30.0})],
        class_final=[("Core", 100.0, "CSPX")],  # only Core names instruments
    )
    out = target_values_by_symbol(doc, total=1000.0, as_of=date(2026, 6, 1))
    assert out["CSPX"] == 700.0
    assert out[UNMAPPED_BUCKET] == 300.0
    assert round(sum(out.values()), 2) == 1000.0  # conservation


def test_target_values_raises_when_class_pct_not_100():
    from datetime import date
    import pytest
    from argosy.services.allocation_engine import target_values_by_symbol
    doc = _doc(glide_dates_pct=[(date(2026, 1, 1), {"Core": 70.0})],
               class_final=[("Core", 100.0, "CSPX")])
    with pytest.raises(ValueError):
        target_values_by_symbol(doc, total=1000.0, as_of=date(2026, 6, 1))


def test_tradeable_holdings_filters_cash_and_nontradeable():
    from argosy.services.allocation_engine import tradeable_holdings

    class P:  # minimal stand-in for PortfolioPosition
        def __init__(self, symbol, usd, asset_type="equity"):
            self.symbol = symbol; self.usd_value_k = usd / 1000.0
            self.asset_type = asset_type

    class Snap:
        positions = [P("CSPX", 600.0), P("IB01", 400.0), P("-", 250.0, "cash"),
                     P("", 0.0), P("CASHUSD", 250.0, "cash")]

    holdings, cash = tradeable_holdings(Snap())
    assert holdings == {"CSPX": 600.0, "IB01": 400.0}
    assert cash == 500.0  # both cash rows aggregated, kept out of holdings


def test_cash_only_deploy_never_trims_and_caps_at_cash():
    from datetime import date
    from argosy.services.allocation_engine import cash_only_deploy
    # codex case: A=70, B=30, target 50/50, cash=10 -> buy only $10 of B, no trim of A
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1), {"A": 50.0, "B": 50.0})],
        class_final=[("A", 50.0, "A"), ("B", 50.0, "B")],
    )
    cands = cash_only_deploy(doc, {"A": 70.0, "B": 30.0}, cash_usd=10.0,
                             as_of=date(2026, 6, 1), account_id="ibkr")
    # only one BUY leg, for B, exactly $10, funded by cash; A untouched
    assert len(cands) == 1
    leg = cands[0].legs[0]
    assert (leg.side, leg.symbol, leg.notional_usd, leg.funding_source) == \
           ("BUY", "B", 10.0, "cash")
    assert all(l.side != "SELL" for c in cands for l in c.legs)


def test_cash_only_deploy_rations_proportionally_when_cash_short():
    from datetime import date
    from argosy.services.allocation_engine import cash_only_deploy
    # both under target by equal gaps; cash less than total gap -> split 50/50
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1), {"A": 50.0, "B": 50.0})],
        class_final=[("A", 50.0, "A"), ("B", 50.0, "B")],
    )
    cands = cash_only_deploy(doc, {"A": 0.0, "B": 0.0}, cash_usd=100.0,
                             as_of=date(2026, 6, 1), account_id="ibkr")
    by = {c.legs[0].symbol: c.legs[0].notional_usd for c in cands}
    assert by == {"A": 50.0, "B": 50.0}
    assert round(sum(by.values()), 2) == 100.0


def test_cash_only_deploy_never_buys_the_unmapped_bucket():
    """The transitional/redeploy band is not a named instrument — cash is never
    deployed into it even though it carries target value (codex #1 follow-on)."""
    from datetime import date
    from argosy.services.allocation_engine import cash_only_deploy, UNMAPPED_BUCKET
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1),
                          {"Core": 50.0,
                           "Individual Stocks (non-NVDA, to redeploy)": 50.0})],
        class_final=[("Core", 100.0, "CSPX")],
    )
    cands = cash_only_deploy(doc, {}, cash_usd=100.0, as_of=date(2026, 6, 1))
    symbols = {l.symbol for c in cands for l in c.legs}
    assert UNMAPPED_BUCKET not in symbols
    assert symbols == {"CSPX"}  # only the named instrument is bought


def test_rebalance_pairs_trim_and_add_into_one_swap():
    from datetime import date
    from argosy.services.allocation_engine import rebalance_candidates
    # plan targets FUSA 100%; holdings are all SCHD -> trim SCHD + add FUSA,
    # and SCHD->FUSA is in REPLACES_SYMBOLS, so it becomes ONE SWAP candidate.
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1), {"Dividend": 100.0})],
        class_final=[("Dividend", 100.0, "FUSA")],
    )
    cands = rebalance_candidates(doc, {"SCHD": 1000.0}, as_of=date(2026, 6, 1),
                                 account_id="leumi")
    swaps = [c for c in cands if c.kind == "SWAP"]
    assert len(swaps) == 1
    sides = {l.symbol: l.side for l in swaps[0].legs}
    assert sides == {"SCHD": "SELL", "FUSA": "BUY"}
    # legs reconcile: sell notional ~= buy notional
    sell = next(l.notional_usd for l in swaps[0].legs if l.side == "SELL")
    buy = next(l.notional_usd for l in swaps[0].legs if l.side == "BUY")
    assert abs(sell - buy) < 1.0


def test_rebalance_unpaired_trim_and_add_stay_separate():
    from datetime import date
    from argosy.services.allocation_engine import rebalance_candidates
    # holding XYZ (not in plan, not in replacement map) -> standalone TRIM
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1), {"Core": 100.0})],
        class_final=[("Core", 100.0, "CSPX")],
    )
    cands = rebalance_candidates(doc, {"XYZ": 500.0, "CSPX": 500.0},
                                 as_of=date(2026, 6, 1), account_id="ibkr")
    kinds = sorted(c.kind for c in cands)
    assert "TRIM" in kinds and "BUY" in kinds and "SWAP" not in kinds


def test_rebalance_unequal_swap_emits_residual_trim_and_conserves():
    """codex #3: when the trim leg exceeds the paired buy, decrement both legs by
    the paired amount and emit the residual as a standalone TRIM — never drop it.
    SCHD 1000 vs FUSA target 600 -> SWAP 600 + residual TRIM 400; Σ SELL == 1000."""
    from datetime import date
    from argosy.services.allocation_engine import rebalance_candidates
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1), {"Dividend": 60.0, "Core": 40.0})],
        class_final=[("Dividend", 60.0, "FUSA"), ("Core", 40.0, "CSPX")],
    )
    cands = rebalance_candidates(doc, {"SCHD": 1000.0}, as_of=date(2026, 6, 1))
    swaps = [c for c in cands if c.kind == "SWAP"]
    trims = [c for c in cands if c.kind == "TRIM"]
    assert len(swaps) == 1
    swap_sell = next(l.notional_usd for l in swaps[0].legs if l.side == "SELL")
    assert round(swap_sell, 2) == 600.0
    schd_trim = sum(l.notional_usd for c in trims for l in c.legs
                    if l.symbol == "SCHD")
    assert round(schd_trim, 2) == 400.0
    # conservation: every SCHD SELL dollar is accounted for (no leg dropped)
    total_sell = sum(l.notional_usd for c in cands for l in c.legs
                     if l.side == "SELL" and l.symbol == "SCHD")
    assert round(total_sell, 2) == 1000.0


def test_cash_only_deploy_sum_never_exceeds_cash_under_rounding():
    """codex bug 1: independent per-leg rounding could push the buy total over
    cash (7 classes at 100/7%, cash=100 -> 7x$14.29 = $100.03). The running
    budget must keep Σ buys <= cash."""
    from datetime import date
    from argosy.services.allocation_engine import cash_only_deploy
    n = 7
    pct = round(100.0 / n, 6)
    comp = {f"C{i}": pct for i in range(n)}
    # nudge the last so the waypoint sums to exactly 100
    comp[f"C{n-1}"] = round(100.0 - pct * (n - 1), 6)
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1), comp)],
        class_final=[(f"C{i}", comp[f"C{i}"], f"C{i}") for i in range(n)],
    )
    cands = cash_only_deploy(doc, {}, cash_usd=100.0, as_of=date(2026, 6, 1))
    total = round(sum(l.notional_usd for c in cands for l in c.legs), 2)
    assert total <= 100.0


def test_rebalance_holds_unmapped_legacy_band_not_liquidates_it():
    """codex bug 2: a legacy holding that belongs to the glide's unmapped/redeploy
    band must be held at the band's current glide weight, not force-exited."""
    from datetime import date
    from argosy.services.allocation_engine import rebalance_candidates
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1),
                          {"Core": 70.0,
                           "Individual Stocks (non-NVDA, to redeploy)": 30.0})],
        class_final=[("Core", 70.0, "CSPX"), ("Bonds", 30.0, "IB01")],
    )
    cands = rebalance_candidates(doc, {"CSPX": 700.0, "GOOG": 300.0},
                                 as_of=date(2026, 6, 1))
    # GOOG sits exactly at the 30% redeploy-band weight -> no trade at all
    assert cands == []


def test_rebalance_plus_cash_no_buy_then_sell_with_unmapped_band():
    """codex bug 3: deploying cash then rebalancing must not buy an instrument
    and immediately sell part of it back."""
    from datetime import date
    from argosy.services.allocation_engine import compute_allocation, AllocationMode
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1),
                          {"Core": 50.0,
                           "Individual Stocks (non-NVDA, to redeploy)": 50.0})],
        class_final=[("Core", 50.0, "CSPX"), ("Bonds", 50.0, "IB01")],
    )
    cands = compute_allocation(doc, {}, AllocationMode.REBALANCE_PLUS_CASH,
                               cash_usd=100.0, as_of=date(2026, 6, 1))
    # net per symbol: no symbol is both bought and sold
    buys = {l.symbol for c in cands for l in c.legs if l.side == "BUY"}
    sells = {l.symbol for c in cands for l in c.legs if l.side == "SELL"}
    assert not (buys & sells)


def test_rebalance_fully_swaps_us_source_not_treats_as_band():
    """codex r2 bug 1: a held US-domiciled symbol whose UCITS twin is a named
    target (VOO->CSPX) must be FULLY swapped, not partially protected as the
    redeploy band. The genuine legacy single (GOOG) is the band."""
    from datetime import date
    from argosy.services.allocation_engine import rebalance_candidates
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1),
                          {"Core": 70.0,
                           "Individual Stocks (non-NVDA, to redeploy)": 30.0})],
        class_final=[("Core", 70.0, "CSPX")],
    )
    cands = rebalance_candidates(doc, {"VOO": 700.0, "GOOG": 300.0},
                                 as_of=date(2026, 6, 1))
    swaps = [c for c in cands if c.kind == "SWAP"]
    assert len(swaps) == 1
    swap_sell = next(l.notional_usd for l in swaps[0].legs
                     if l.side == "SELL" and l.symbol == "VOO")
    assert round(swap_sell, 2) == 700.0  # all of VOO swapped to CSPX
    assert [l for c in cands for l in c.legs if l.symbol == "GOOG"] == []  # band held


def test_pure_rebalance_does_not_create_unfunded_buys():
    """codex r2 bug 2: per-symbol keep-band must not drop offsetting trims while
    keeping an over-band buy (unfunded buying power). Σ buys <= Σ sells."""
    from datetime import date
    from argosy.services.allocation_engine import rebalance_candidates
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1), {"A": 50.0, "B": 25.0, "C": 25.0})],
        class_final=[("A", 50.0, "A"), ("B", 25.0, "B"), ("C", 25.0, "C")],
    )
    cands = rebalance_candidates(doc, {"A": 485000.0, "B": 257500.0, "C": 257500.0},
                                 as_of=date(2026, 6, 1))
    buys = sum(l.notional_usd for c in cands for l in c.legs if l.side == "BUY")
    sells = sum(l.notional_usd for c in cands for l in c.legs if l.side == "SELL")
    assert round(buys, 2) <= round(sells, 2) + 0.01  # closed-book funded
    assert round(buys, 2) == 15000.0 and round(sells, 2) == 15000.0


def test_compute_allocation_dispatches_modes():
    from datetime import date
    from argosy.services.allocation_engine import compute_allocation, AllocationMode
    doc = _doc(
        glide_dates_pct=[(date(2026, 1, 1), {"Core": 100.0})],
        class_final=[("Core", 100.0, "CSPX")],
    )
    holdings = {"CSPX": 1000.0}
    # cash-only deploy: a pure buy
    c1 = compute_allocation(doc, holdings, AllocationMode.CASH_ONLY_DEPLOY,
                            cash_usd=500.0, as_of=date(2026, 6, 1))
    assert c1 and all(l.side == "BUY" for c in c1 for l in c.legs)
    # pure rebalance with on-target book: nothing to do
    c2 = compute_allocation(doc, holdings, AllocationMode.PURE_REBALANCE,
                            as_of=date(2026, 6, 1))
    assert c2 == []
