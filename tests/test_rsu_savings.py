"""Deterministic contractual-RSU net savings (B1). The known_grants_only savings floor
is computable from the contractual vest calendar × price × FX × at-vest retention — not
an LLM estimate (whose run-to-run variance caused a 43% swing). Pinned to EXACTLY 5
calendar years (the prior "2026-2031" prompt spanned 6)."""
from __future__ import annotations

from datetime import date

import pytest

from argosy.services.rsu_savings import (
    contractual_rsu_net_by_year,
    project_quarterly_vests,
)


# Ariel's 6 NVIDIA grants (identity_yaml.rsu_grants) + the authoritative portal calendar.
_ACTIVE_GRANTS = [
    {"award_id": "213000", "award_date": "2022-06-08", "quarterly_shares": 0},
    {"award_id": "246477", "award_date": "2023-06-08", "quarterly_shares": 220},
    {"award_id": "289172", "award_date": "2024-04-08", "quarterly_shares": 20},
    {"award_id": "289173", "award_date": "2024-04-08", "quarterly_shares": 83},
    {"award_id": "331375", "award_date": "2025-03-10", "quarterly_shares": 71},
    {"award_id": "374434", "award_date": "2026-03-09", "quarterly_shares": 57},
]
_PORTAL = [
    {"date": "2026-06-17", "shares": 729},
    {"date": "2026-09-16", "shares": 449},
    {"date": "2026-12-09", "shares": 460},
    {"date": "2027-03", "shares": 450},
]


def _bucket(events):
    out = {}
    for e in events:
        y = int(e["date"][:4])
        out[y] = out.get(y, 0) + e["shares"]
    return out


def test_projection_reproduces_codex_share_vector():
    # codex-reviewed oracle: portal override for 2026-Jun..2027-Mar (incl. the +278
    # one-time 2022-grant runoff in June), grant-runoff projection thereafter, forward
    # window from 2026-06 (March 2026 already realized -> excluded).
    events = project_quarterly_vests(
        _ACTIVE_GRANTS, _PORTAL, horizon_start_year=2026, horizon_years=5)
    assert _bucket(events) == {2026: 1638, 2027: 1363, 2028: 615, 2029: 299, 2030: 57}


def test_projected_net_vector_matches_codex_at_capital_track_retention():
    events = project_quarterly_vests(
        _ACTIVE_GRANTS, _PORTAL, horizon_start_year=2026, horizon_years=5)
    by_year, _avg = contractual_rsu_net_by_year(
        events, nvda_price_usd=205.0, usd_nis_fx=2.88, at_vest_retention=0.68,
        horizon_start_year=2026, horizon_years=5)
    # net/share = 205*2.88*0.68 = 401.472 ; 2026 = 1638 * 401.472
    assert by_year[2026] == pytest.approx(657_611, abs=2)
    assert by_year[2027] == pytest.approx(547_206, abs=2)
    assert by_year[2030] == pytest.approx(22_884, abs=2)
    # front-loaded: 2026 net is well above the flat ~312k LLM floor it replaces.
    assert by_year[2026] > 600_000


def test_projection_excludes_realized_pre_window_vests():
    # No event before the earliest portal date (2026-06) — March-2026 quarter dropped.
    events = project_quarterly_vests(
        _ACTIVE_GRANTS, _PORTAL, horizon_start_year=2026, horizon_years=5)
    assert all(e["date"] >= "2026-06" for e in events)


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
