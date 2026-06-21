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


# --- B1: per-year savings vector ----------------------------------------------
# The contractual RSU vest stream is front-loaded (grants run off 2026->2030), so
# a flat 5-yr mean understates the early window where the crossing falls and pushes
# the crossing LATER. The vector path feeds year-specific savings; years outside the
# vector fall back to ``annual_real_savings`` (the post-window tail floor).

def test_constant_vector_matches_scalar_path():
    # A vector that is constant at the scalar value reproduces the closed-form
    # scalar crossing exactly — the iterative vector path is consistent with the
    # annuity formula (guards against an off-by-one in the year indexing).
    kw = dict(liquid_now=11_300_000.0, fi_total=11_836_133.0,
              real_return=0.03, current_year=2026)
    scalar = fi_crossing_year(annual_real_savings=300_000.0, **kw)
    vec = fi_crossing_year(
        annual_real_savings=0.0,
        savings_by_year={y: 300_000.0 for y in range(2026, 2090)}, **kw)
    assert scalar == vec == 2027


def test_front_loaded_vector_crosses_earlier_than_its_flat_mean():
    # Same 5-yr total (₪1.0M) delivered all in year 1 vs spread flat at the mean.
    # With a positive real return the early money compounds longer, so the
    # front-loaded stream crosses a year sooner. Growth alone does NOT clear the
    # target before either crossing (9M*1.03^2 = 9.55M < 10.3M), so savings are
    # the operative difference in both.
    kw = dict(liquid_now=9_000_000.0, fi_total=10_300_000.0,
              real_return=0.05, current_year=2026)
    flat = fi_crossing_year(annual_real_savings=200_000.0, **kw)
    front = fi_crossing_year(
        annual_real_savings=0.0, savings_by_year={2026: 1_000_000.0}, **kw)
    assert front == 2027
    assert flat == 2028
    assert front < flat


def test_vector_uses_scalar_as_post_window_tail():
    # A vector covering only the contractual window; years past it fall back to
    # the scalar floor (the post-window tail). Here the window contributes nothing
    # and the tail (300k) drives the crossing — proving the fallback is applied.
    yr = fi_crossing_year(
        liquid_now=11_300_000.0, fi_total=11_836_133.0, real_return=0.03,
        annual_real_savings=300_000.0, current_year=2026,
        savings_by_year={2026: 0.0})  # only 2026 pinned to 0; 2027+ -> tail 300k
    # 2026 contributes 0 -> FV(1)=11.639M (<target). 2027 tail 300k -> FV(2) clears.
    assert yr == 2028
