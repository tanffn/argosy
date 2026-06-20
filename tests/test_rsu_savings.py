"""Deterministic contractual-RSU net savings (B1). The known_grants_only savings floor
is computable from the contractual vest calendar × price × FX × at-vest retention — not
an LLM estimate (whose run-to-run variance caused a 43% swing). Pinned to EXACTLY 5
calendar years (the prior "2026-2031" prompt spanned 6)."""
from __future__ import annotations

from datetime import date

from argosy.services.rsu_savings import contractual_rsu_net_by_year


def test_sums_each_vest_into_its_calendar_year_net_of_retention():
    vests = [
        {"date": "2026-06-17", "shares": 700},
        {"date": "2026-12-09", "shares": 300},   # 1000 shares in 2026
        {"date": "2027-03-17", "shares": 500},    # 500 in 2027
    ]
    by_year, avg = contractual_rsu_net_by_year(
        vests, nvda_price_usd=200.0, usd_nis_fx=3.0, at_vest_retention=0.5,
        horizon_start_year=2026, horizon_years=5,
    )
    # 2026: 1000 sh * 200 * 3.0 * 0.5 = 300,000 ; 2027: 500*200*3*0.5 = 150,000
    assert by_year[2026] == 300_000.0
    assert by_year[2027] == 150_000.0
    # horizon is EXACTLY 5 years (2026-2030); empty years count as 0 in the mean.
    assert set(by_year) == {2026, 2027, 2028, 2029, 2030}
    assert avg == (300_000.0 + 150_000.0) / 5


def test_vests_outside_the_pinned_horizon_are_excluded():
    vests = [
        {"date": "2025-06-17", "shares": 1000},   # before horizon → excluded
        {"date": "2031-06-17", "shares": 1000},   # after 5-yr horizon → excluded
        {"date": "2028-06-17", "shares": 100},
    ]
    by_year, avg = contractual_rsu_net_by_year(
        vests, nvda_price_usd=100.0, usd_nis_fx=4.0, at_vest_retention=1.0,
        horizon_start_year=2026, horizon_years=5,
    )
    assert by_year[2028] == 100 * 100.0 * 4.0
    assert 2025 not in by_year and 2031 not in by_year
    assert avg == (100 * 100.0 * 4.0) / 5


def test_is_deterministic_and_handles_date_objects_and_bad_rows():
    vests = [
        {"date": date(2026, 6, 17), "shares": 200},
        {"date": "not-a-date", "shares": 50},      # skipped
        {"shares": 50},                              # no date → skipped
        {"date": "2026-09-16"},                      # no shares → skipped
    ]
    a = contractual_rsu_net_by_year(vests, nvda_price_usd=10.0, usd_nis_fx=3.0,
                                    at_vest_retention=0.7, horizon_start_year=2026)
    b = contractual_rsu_net_by_year(vests, nvda_price_usd=10.0, usd_nis_fx=3.0,
                                    at_vest_retention=0.7, horizon_start_year=2026)
    assert a == b                                    # deterministic
    assert a[0][2026] == 200 * 10.0 * 3.0 * 0.7      # only the one valid row counted


def test_five_year_mean_divides_by_five_not_count_of_nonzero_years():
    vests = [{"date": "2026-06-17", "shares": 1000}]
    _by_year, avg = contractual_rsu_net_by_year(
        vests, nvda_price_usd=100.0, usd_nis_fx=1.0, at_vest_retention=1.0,
        horizon_start_year=2026, horizon_years=5,
    )
    assert avg == 100_000.0 / 5   # NOT / 1
