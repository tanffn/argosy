from argosy.services.deployment_funnel.reserve import (
    existing_cash_like_usd,
    reserve_shortfall_usd,
)


def test_existing_cash_like_sums_sgov_and_cash():
    holdings = {"SGOV": 127040.0, "CASH_USD": 144940.0, "CSPX": 156820.0}
    assert existing_cash_like_usd(holdings) == 127040.0 + 144940.0


def test_reserve_already_funded_zero_shortfall():
    # Book $4.06M, 6% reserve target = ~$243k; existing cash-like $272k -> funded.
    holdings = {"SGOV": 127040.0, "CASH_USD": 144940.0}
    assert reserve_shortfall_usd(4_060_000.0, holdings, 6.0) == 0.0


def test_reserve_shortfall_when_underfunded():
    holdings = {"SGOV": 10000.0}
    # 6% of 1,000,000 = 60,000; existing 10,000 -> 50,000 shortfall.
    assert reserve_shortfall_usd(1_000_000.0, holdings, 6.0) == 50000.0
