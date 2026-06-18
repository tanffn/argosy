"""Tests for argosy.quality.graph_collections — COLLECTION nodes + resolver-
backed re-derivation. The crux: a DERIVED node that depends on a COLLECTION
INPUT node re-derives when the collection's MEMBERSHIP changes (a position
added/removed), not just when a scalar input moves. This closes the
"stale-but-valid" dependency-completeness hole: previously the headline
estate figure was a hand-set scalar with no edge to the holdings that produced
it, so adding a US position left it falsely valid.

Pure — no DB, no LLM. Positions/fx are passed in.
"""
from __future__ import annotations

from argosy.quality.derivation_graph import NodeKind
from argosy.quality.graph_collections import build_holdings_graph
from argosy.services.retirement.safety_gates import _us_situs_assets_usd


# A small but representative holdings list: a US-domiciled single name (NVDA,
# estate_safe_for == False), a UCITS-marked ETF (estate-safe via the explicit
# marker), USD cash (excluded), and a NIS-native cash row.
def _base_positions() -> list[dict]:
    return [
        {
            "symbol": "NVDA",
            "details": "NVIDIA Corp",
            "asset_type": "Stock",
            "currency": "USD",
            "usd_value_k": 2000.0,
        },
        {
            "symbol": "VWRA",
            "details": "Vanguard FTSE All-World UCITS ETF",
            "asset_type": "ETF",
            "currency": "USD",
            "usd_value_k": 300.0,
        },
        {
            "symbol": "",
            "details": "USD cash buffer",
            "asset_type": "Cash",
            "currency": "USD",
            "usd_value_k": 100.0,
        },
        {
            "symbol": "",
            "details": "NIS cash",
            "asset_type": "Cash",
            "currency": "NIS",
            "usd_value_k": 50.0,
        },
    ]


FX = 3.7


def test_holdings_is_a_collection_input_node():
    positions = _base_positions()
    g = build_holdings_graph(positions, FX)
    holdings = g.get("holdings")
    assert holdings.kind is NodeKind.INPUT
    assert holdings.value == positions
    fx = g.get("fx.usd_nis")
    assert fx.kind is NodeKind.INPUT
    assert fx.value == FX


def test_derived_us_situs_matches_authoritative_function():
    positions = _base_positions()
    g = build_holdings_graph(positions, FX)
    g.recompute()
    node = g.get("concentration.us_situs_estate_nis")
    assert node.kind is NodeKind.DERIVED
    # "holdings" is an inbound edge — the set-edge that closes the hole.
    assert "holdings" in node.inputs
    assert "fx.usd_nis" in node.inputs
    expected = _us_situs_assets_usd(positions) * FX
    assert node.value == expected
    # NVDA only (VWRA is UCITS, cash excluded): 2000k USD * 1000 * fx.
    assert node.value == 2000.0 * 1000.0 * FX


def test_symbol_breakdown_classifies_each_holding():
    positions = _base_positions()
    g = build_holdings_graph(positions, FX)
    g.recompute()
    breakdown = g.get("concentration.us_situs_symbol_breakdown").value
    assert isinstance(breakdown, list)
    assert len(breakdown) == len(positions)
    by_sym = {(r["symbol"], r["name"]): r for r in breakdown}
    # NVDA -> US, included; carries the human name from name_for.
    nvda = next(r for r in breakdown if r["symbol"] == "NVDA")
    assert nvda["classification"] == "US"
    assert nvda["name"] == "NVIDIA"
    assert nvda["usd_value"] == 2000.0 * 1000.0
    # VWRA -> non-US (UCITS marker), excluded from the estate sum.
    vwra = next(r for r in breakdown if r["symbol"] == "VWRA")
    assert vwra["classification"] == "non-US"
    # Cash rows -> cash.
    cash_rows = [r for r in breakdown if r["classification"] == "cash"]
    assert len(cash_rows) == 2
    # The included US value reconciles to the authoritative function.
    included_us = sum(
        r["usd_value"] for r in breakdown if r["classification"] == "US"
    )
    assert included_us == _us_situs_assets_usd(positions)
    _ = by_sym  # silence unused


def test_membership_edge_adding_us_position_changes_us_situs():
    """THE crux: set_input on the collection with an extra US holding must
    invalidate + re-derive us_situs to a LARGER value. Membership edge works."""
    positions = _base_positions()
    g = build_holdings_graph(positions, FX)
    g.recompute()
    before = g.get("concentration.us_situs_estate_nis").value
    assert g.is_closed()

    new_us = {
        "symbol": "AMD",
        "details": "Advanced Micro Devices",
        "asset_type": "Stock",
        "currency": "USD",
        "usd_value_k": 500.0,
    }
    invalidated = g.set_input("holdings", positions + [new_us])
    # The derived estate node is now stale (not falsely valid).
    assert "concentration.us_situs_estate_nis" in invalidated
    assert not g.is_valid("concentration.us_situs_estate_nis")

    g.recompute()
    after = g.get("concentration.us_situs_estate_nis").value
    assert after > before
    assert after == before + 500.0 * 1000.0 * FX
    # Breakdown grew by one row.
    assert len(g.get("concentration.us_situs_symbol_breakdown").value) == len(positions) + 1
    assert g.is_closed()


def test_fx_scalar_edge_rederives_us_situs():
    positions = _base_positions()
    g = build_holdings_graph(positions, FX)
    g.recompute()
    before = g.get("concentration.us_situs_estate_nis").value
    g.set_input("fx.usd_nis", FX * 2)
    g.recompute()
    after = g.get("concentration.us_situs_estate_nis").value
    assert after == before * 2


def test_net_worth_and_nvda_pct_derived_from_holdings():
    positions = _base_positions()
    g = build_holdings_graph(positions, FX)
    g.recompute()
    nw = g.get("portfolio.net_worth_nis").value
    # USD assets (2000+300+100 = 2400k) * 1000 * fx + NIS native (50k * 1000).
    expected_nw = 2400.0 * 1000.0 * FX + 50.0 * 1000.0
    assert nw == expected_nw

    nvda = g.get("concentration.nvda_current_pct").value
    # NVDA 2000k / tradeable book (NVDA 2000 + VWRA 300 = 2300k) -> fraction 0-1.
    from argosy.services.wealth_dashboard import nvda_concentration_pct
    assert abs(nvda - nvda_concentration_pct(positions) / 100.0) < 1e-12

    # Both depend on the holdings collection.
    assert "holdings" in g.get("portfolio.net_worth_nis").inputs
    assert "holdings" in g.get("concentration.nvda_current_pct").inputs


def test_nvda_pct_rederives_when_membership_changes():
    positions = _base_positions()
    g = build_holdings_graph(positions, FX)
    g.recompute()
    before = g.get("concentration.nvda_current_pct").value
    # Add a big non-NVDA US holding -> NVDA's share of the book drops.
    g.set_input("holdings", positions + [{
        "symbol": "AMD", "details": "AMD", "asset_type": "Stock",
        "currency": "USD", "usd_value_k": 5000.0,
    }])
    g.recompute()
    after = g.get("concentration.nvda_current_pct").value
    assert after < before


def test_graph_is_acyclic_and_closes():
    g = build_holdings_graph(_base_positions(), FX)
    g.check_acyclic()
    g.recompute()
    assert g.is_closed()
