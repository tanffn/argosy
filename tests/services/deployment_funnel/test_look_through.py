import json
import sqlite3
from pathlib import Path

import pytest

from argosy.services.deployment_funnel.look_through import (
    LOOKTHROUGH_MAP,
    effective_nvda_usd,
    effective_us_usd,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEV_DB = _REPO_ROOT / "db" / "argosy.db"


def test_cspx_carries_sp500_nvda_weight():
    # CSPX ~7% NVDA: $22,000 -> ~$1,540 effective NVDA.
    assert effective_nvda_usd("CSPX", 22000.0) == pytest.approx(1540.0, abs=1.0)


def test_r1gr_carries_higher_growth_nvda_weight():
    # R1GR ~14% NVDA (plan's own rationale): $13,000 -> ~$1,820.
    assert effective_nvda_usd("R1GR", 13000.0) == pytest.approx(1820.0, abs=1.0)


def test_gold_and_tbills_carry_zero_nvda():
    assert effective_nvda_usd("SGLD", 45000.0) == 0.0
    assert effective_nvda_usd("IB01", 3000.0) == 0.0


def test_direct_nvda_is_full_weight():
    assert effective_nvda_usd("NVDA", 5000.0) == 5000.0


def test_unknown_symbol_assumes_zero_lookthrough():
    assert effective_nvda_usd("XYZ", 1000.0) == 0.0


def test_us_weight_for_world_fund_is_partial():
    assert effective_us_usd("FWRA", 10000.0) == pytest.approx(6500.0, abs=1.0)
    assert "CSPX" in LOOKTHROUGH_MAP


# --- Spot checks for the v2 completeness pass entries -----------------------


def test_dpya_is_world_property_not_us_dividend():
    # The live mis-guess: DPYA is developed-world property (FTSE EPRA/NAREIT,
    # same index as held IWDP), ~64% US by geography and ZERO NVDA — not a US
    # quality-dividend fund.
    assert effective_nvda_usd("DPYA", 10000.0) == 0.0
    assert effective_us_usd("DPYA", 10000.0) == pytest.approx(6400.0, abs=1.0)


def test_semiconductor_thematic_carries_real_nvda_weight():
    # SMGB (VanEck Semiconductor UCITS) holds ~7% NVDA — a sleeve seed that
    # MUST count against the concentration cap.
    assert effective_nvda_usd("SMGB", 10000.0) == pytest.approx(700.0, abs=1.0)
    assert effective_nvda_usd("WTAI", 10000.0) == pytest.approx(400.0, abs=1.0)


def test_us_single_names_are_full_us_zero_nvda():
    assert effective_us_usd("SPCX", 5000.0) == 5000.0  # SpaceX — US company
    assert effective_nvda_usd("SPCX", 5000.0) == 0.0
    assert effective_us_usd("BRK/B", 5000.0) == 5000.0
    assert effective_us_usd("OKLO", 5000.0) == 5000.0


def test_non_us_single_names_carry_zero_us_weight():
    assert effective_us_usd("MELI", 5000.0) == 0.0  # LatAm economics
    assert effective_us_usd("NU", 5000.0) == 0.0    # Brazil
    assert effective_us_usd("INVZ", 5000.0) == 0.0  # Israel


# --- Completeness: the map may never silently fall behind the plan ----------


def _code_plan_menu_symbols() -> set[str]:
    """Every instrument in the code-level allocation menu (the plan's sleeves)."""
    from argosy.services import allocation_plan as ap

    sleeves = list(ap._EQUITY_SLEEVES) + [ap._NVDA_SLEEVE, ap._FI_CASH, ap._FI_BONDS]
    return {
        inst.symbol.upper() for sleeve in sleeves for inst in sleeve.instruments
    }


def _high_potential_seed_symbols() -> set[str]:
    from argosy.services.high_potential_sleeve import _SEED_CANDIDATES

    return {c.ticker.upper() for c in _SEED_CANDIDATES}


def _collect_symbols(node) -> set[str]:
    """Recursively collect every 'symbol' value from a JSON blob."""
    out: set[str] = set()
    if isinstance(node, dict):
        sym = node.get("symbol")
        if isinstance(sym, str):
            out.add(sym.upper())
        for value in node.values():
            out |= _collect_symbols(value)
    elif isinstance(node, list):
        for item in node:
            out |= _collect_symbols(item)
    return out


def test_code_plan_menu_fully_covered():
    missing = _code_plan_menu_symbols() - set(LOOKTHROUGH_MAP)
    assert not missing, (
        f"Plan instrument menu symbols missing from LOOKTHROUGH_MAP: {sorted(missing)}. "
        "Blind reviewers get NO facts for these and are forced onto unreliable "
        "world knowledge — add cited entries to look_through.py."
    )


def test_high_potential_seeds_fully_covered():
    missing = _high_potential_seed_symbols() - set(LOOKTHROUGH_MAP)
    assert not missing, (
        f"High-potential sleeve seeds missing from LOOKTHROUGH_MAP: {sorted(missing)}"
    )


@pytest.mark.skipif(not _DEV_DB.exists(), reason="dev DB not present")
def test_live_plan_versions_fully_covered():
    """Every instrument in the current + draft plan versions (incl. authored
    overrides that exist only in the DB, e.g. the v64 high-growth names) has a
    look-through entry."""
    con = sqlite3.connect(_DEV_DB)
    try:
        rows = con.execute(
            "SELECT target_allocation_json, target_allocation_overrides_json "
            "FROM plan_versions WHERE role IN ('current', 'draft')"
        ).fetchall()
    finally:
        con.close()
    symbols: set[str] = set()
    for row in rows:
        for blob in row:
            if blob:
                symbols |= _collect_symbols(json.loads(blob))
    assert symbols, "expected at least one plan version with instruments"
    missing = symbols - set(LOOKTHROUGH_MAP)
    assert not missing, (
        f"Plan-version instruments missing from LOOKTHROUGH_MAP: {sorted(missing)}"
    )


@pytest.mark.skipif(not _DEV_DB.exists(), reason="dev DB not present")
def test_held_positions_fully_covered():
    """Every held security in the latest portfolio snapshot has a look-through
    entry (cash / real-estate placeholder rows excluded)."""
    con = sqlite3.connect(_DEV_DB)
    try:
        row = con.execute(
            "SELECT positions_json FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    assert row and row[0], "expected a portfolio snapshot"
    held = {
        (p.get("symbol") or "").upper()
        for p in json.loads(row[0])
        if (p.get("symbol") or "").strip() not in ("", "-")
    }
    assert held, "expected held positions in the snapshot"
    missing = held - set(LOOKTHROUGH_MAP)
    assert not missing, (
        f"Held positions missing from LOOKTHROUGH_MAP: {sorted(missing)}"
    )
