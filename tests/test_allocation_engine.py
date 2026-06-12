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
