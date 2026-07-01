import pytest

from argosy.services.deployment_funnel.look_through import (
    LOOKTHROUGH_MAP,
    effective_nvda_usd,
    effective_us_usd,
)


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
