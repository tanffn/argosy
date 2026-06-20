# tests/test_fi_crossing.py
import pytest
from argosy.services.fi_crossing import fi_crossing_year


def test_already_reached_is_current_year():
    # liquid already >= target -> crosses now (margin >= 0).
    assert fi_crossing_year(liquid_now=12_000_000, fi_total=11_836_133,
                            real_return=0.03, annual_real_savings=300_000,
                            current_year=2026) == 2026


def test_savings_are_actually_included_in_crossing():
    # CODEX #6: growth ALONE must NOT clear the target, so the test proves savings
    # matter. 11.30M * 1.03 = 11.639M < 11.836M (growth alone short); + 300k = 11.939M
    # clears -> 2027. (If savings were ignored, n=1 would fail and it'd be 2028.)
    yr = fi_crossing_year(liquid_now=11_300_000, fi_total=11_836_133,
                          real_return=0.03, annual_real_savings=300_000,
                          current_year=2026)
    assert yr == 2027
    # guard: growth alone at n=1 is below target (proves savings were the difference)
    assert 11_300_000 * 1.03 < 11_836_133


def test_zero_return_uses_linear_savings():
    yr = fi_crossing_year(liquid_now=11_000_000, fi_total=11_900_000,
                          real_return=0.0, annual_real_savings=300_000,
                          current_year=2026)
    # need 900k / 300k = 3 years -> 2029.
    assert yr == 2029


def test_never_reached_within_horizon_returns_none():
    assert fi_crossing_year(liquid_now=1_000, fi_total=10_000_000,
                            real_return=0.0, annual_real_savings=0.0,
                            current_year=2026, horizon_years=60) is None


def test_horizon_boundary_inclusive():
    # CODEX #7: exactly reachable at n=60 is included; n=61 returns None.
    # 0 return, savings 1/yr: FV(n) = base + n. target = base + 60 -> n=60 ok.
    base = 1_000_000.0
    assert fi_crossing_year(liquid_now=base, fi_total=base + 60, real_return=0.0,
                            annual_real_savings=1.0, current_year=2026,
                            horizon_years=60) == 2086
    assert fi_crossing_year(liquid_now=base, fi_total=base + 61, real_return=0.0,
                            annual_real_savings=1.0, current_year=2026,
                            horizon_years=60) is None


def test_points_instead_of_fraction_fails_loud():
    # CODEX #1: a real_return of 3.0 (points) must raise, not silently project 300%.
    with pytest.raises(ValueError):
        fi_crossing_year(liquid_now=1.0, fi_total=2.0, real_return=3.0,
                         annual_real_savings=0.0, current_year=2026)
