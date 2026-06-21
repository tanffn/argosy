"""Tests for ``argosy.services.rsu_reconciliation.sim_tax`` — the NVIDIA ESOP
§102 simulation parser + per-grant tax model.

The simulation report is the authoritative per-grant Israeli §102 tax model.
These tests pin the EXACT formula reverse-engineered from the sheet (verified to
0.00 USD residual across all data rows) and the calibration anchors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from argosy.services.rsu_reconciliation.sim_tax import (
    HOLDING_BREAKING,
    HOLDING_CAPITAL,
    parse_sim_report,
)

_SIM_PATH = Path(
    "D:/Google Drive/Family/Finances/Portfolio/Resources/2026/Schwab/"
    "Nvidia simulation Report.xlsx"
)
pytestmark = pytest.mark.skipif(
    not _SIM_PATH.exists(), reason="simulation report not mounted"
)


def test_parses_all_expected_grants() -> None:
    sim = parse_sim_report(_SIM_PATH)
    assert set(sim.grants) == {
        "182406", "213000", "246477", "289172", "289173", "331375", "374434",
    }
    assert sim.simulation_date is not None


def test_holding_period_classification() -> None:
    sim = parse_sim_report(_SIM_PATH)
    assert sim.grant("182406").holding_period == HOLDING_CAPITAL
    assert sim.grant("182406").is_capital_track
    assert sim.grant("331375").holding_period == HOLDING_BREAKING
    assert not sim.grant("331375").is_capital_track
    assert sim.grant("374434").holding_period == HOLDING_BREAKING


def test_amount_wired_reproduced_per_grant_within_1_usd() -> None:
    """For EACH grant, applying net_for_shares to the sim's own aggregate
    quantity + sale price + total fees reproduces the sheet's net "Amount
    Wired" to ±$1 (wire fee folded in)."""
    sim = parse_sim_report(_SIM_PATH)
    for gid, g in sim.grants.items():
        # gross_after_fees == sale_price*shares - fees  → derive broker/trustee
        # fees. The sheet's per-row "Amount Wired" (which net_wired_usd sums)
        # does NOT subtract the one-time wire fee, so pass wire_fee_usd=0 here.
        fees = g.sale_price_usd * g.shares - g.gross_after_fees_usd
        sn = g.net_for_shares(
            g.shares, g.sale_price_usd, fees_usd=fees, wire_fee_usd=0.0,
        )
        assert sn.net_usd == pytest.approx(g.net_wired_usd, abs=1.0), gid
        assert sn.advance_tax_usd == pytest.approx(g.advance_tax_usd, abs=1.0), gid


def test_calibration_anchors() -> None:
    sim = parse_sim_report(_SIM_PATH)
    # 560 sh @ $204.65 grant 182406 → ~$82,135 net
    sn = sim.grant("182406").net_for_shares(560, 204.65, fees_usd=3.3579 + 2.8)
    assert sn.net_usd == pytest.approx(82135.1, abs=1.0)
    # 71 sh @ $204.65 grant 331375 (breaking, 100% ordinary) → ~$5,496 net
    sn2 = sim.grant("331375").net_for_shares(71, 204.65, fees_usd=0.4257 + 0.355)
    assert sn2.net_usd == pytest.approx(5496.5, abs=1.0)
    assert sn2.capital_income_usd == pytest.approx(0.0)


def test_capital_track_split_formula() -> None:
    """ordinary = grant_price*shares - fees ; capital = (price-grant_price)*shares."""
    sim = parse_sim_report(_SIM_PATH)
    g = sim.grant("182406")
    sn = g.net_for_shares(1000, 200.0, fees_usd=10.0)
    assert sn.ordinary_income_usd == pytest.approx(g.grant_price_usd * 1000 - 10.0)
    assert sn.capital_income_usd == pytest.approx((200.0 - g.grant_price_usd) * 1000)
    assert sn.advance_tax_usd == pytest.approx(
        sn.capital_income_usd * 0.25 + sn.ordinary_income_usd * 0.6217, abs=0.5
    )


def test_breaking_grant_is_all_ordinary() -> None:
    sim = parse_sim_report(_SIM_PATH)
    g = sim.grant("374434")
    sn = g.net_for_shares(100, 200.0, fees_usd=5.0)
    assert sn.capital_income_usd == pytest.approx(0.0)
    assert sn.ordinary_income_usd == pytest.approx(200.0 * 100 - 5.0)
    assert sn.advance_tax_usd == pytest.approx(sn.ordinary_income_usd * 0.6217, abs=0.5)


def test_retention_grant_dependent() -> None:
    sim = parse_sim_report(_SIM_PATH)
    rets = {gid: g.effective_retention for gid, g in sim.grants.items()}
    assert rets["182406"] > 0.71
    assert rets["331375"] < 0.40
    # NOT flat: spread of >0.3 between best and worst grant.
    assert max(rets.values()) - min(rets.values()) > 0.30


def test_espp_pool_parsed() -> None:
    sim = parse_sim_report(_SIM_PATH)
    assert sim.espp is not None
    assert sim.espp.shares > 0
    # ESPP is ordinary-heavy (breaking-dominated) → low retention.
    assert sim.espp.effective_retention < 0.60
