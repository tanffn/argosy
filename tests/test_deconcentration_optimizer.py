"""Tests for the NVDA deconcentration sell-rate optimizer (deconcentration_optimizer.py).

Two layers, both seed-noise robust:

  * ``effective_cgt_rate`` / ``total_cgt_for_horizon`` — pure tax math. Exact,
    deterministic; asserts monotonicity, bounds, and the tax-bunching ordering
    (1-year horizon pays more total CGT than a 5-year horizon).
  * ``optimize_deconcentration_core`` — the horizon sweep over the canonical MC.
    Structural only (a finite drawdown age, a chosen horizon in range, a fully
    populated per-horizon table) with small n_paths + a fixed seed so MC noise
    can't flake the suite.
"""
from datetime import date

import pytest

from argosy.services.cashflow_projection import (
    DEFAULT_TAXABLE_GAIN_FRACTION,
    HouseholdState,
    PensionState,
)
from argosy.services.retirement.deconcentration_optimizer import (
    CGT_BASE_RATE,
    CGT_MARGINAL_ABOVE_THRESHOLD,
    SURTAX_THRESHOLD_NIS,
    DeconcentrationPlan,
    effective_cgt_rate,
    optimize_deconcentration_core,
    total_cgt_for_horizon,
)
from argosy.services.retirement.retirement_plan import RetirementAssumptions, _reserve_pv


def _household() -> HouseholdState:
    return HouseholdState(
        monthly_expenses_nis=0.0,          # overridden per-frontier by the core
        portfolio_value_nis=10_992_315.0,  # overridden per-frontier by the core
        fx_usd_nis=2.895,
        current_age_years=44.0,
        monthly_savings_nis=29_916.0,
    )


def _pensions() -> PensionState:
    return PensionState(
        kupat_pensia_balance_nis=800_147.0,
        kupat_pensia_contribution_monthly_nis=0.0,
        executive_insurance_balance_nis=755_907.0,
        keren_hishtalmut_balance_nis=384_000.0,
        keren_hishtalmut_contribution_monthly_nis=0.0,
        kupat_gemel_balance_nis=75_000.0,
    )


FULL = 10_992_315.0
SELL = 5_700_000.0                                   # ~NVDA over-cap sell amount
GAIN = SELL * DEFAULT_TAXABLE_GAIN_FRACTION          # ~₪3.42M taxable real gain


def _plan(**over) -> DeconcentrationPlan:
    a = RetirementAssumptions(n_paths=500, max_age=58, seed=42, **over)
    reserve_pv = _reserve_pv(1_450_000.0, a.reserve_discount_real, a.reserve_avg_liability_years)
    return optimize_deconcentration_core(
        household=_household(), pensions=_pensions(),
        full_portfolio_nis=FULL, reserve_pv_nis=reserve_pv,
        total_taxable_gain_nis=GAIN, sell_nis=SELL,
        nvda_current_pct=0.65, nvda_cap_pct=0.13,
        spend_central_nis=281_584.0, bl_monthly_nis=1_710.0, bl_source="test",
        annuity_tax_rate=0.155, sigma_current=0.3442,
        horizons=(1, 2, 3, 4, 5), target_p_solvent=0.90,
        assumptions=a, today=date(2026, 6, 5),
    )


# --- effective_cgt_rate: pure tax math (exact, deterministic) ----------------

def test_effective_cgt_rate_bounds():
    # Below threshold -> exactly the base rate; above -> never beyond the marginal.
    assert effective_cgt_rate(0.0) == CGT_BASE_RATE
    assert effective_cgt_rate(100_000.0) == pytest.approx(CGT_BASE_RATE)
    assert effective_cgt_rate(SURTAX_THRESHOLD_NIS) == pytest.approx(CGT_BASE_RATE)
    for g in (1.0, 500_000.0, SURTAX_THRESHOLD_NIS, 1_000_000.0, 5_000_000.0, 50_000_000.0):
        r = effective_cgt_rate(g)
        assert CGT_BASE_RATE <= r <= CGT_MARGINAL_ABOVE_THRESHOLD
    # A very large single-year gain blends toward (but never reaches) the marginal.
    assert effective_cgt_rate(1e12) == pytest.approx(CGT_MARGINAL_ABOVE_THRESHOLD, abs=1e-3)


def test_effective_cgt_rate_monotonic_nondecreasing():
    prev = -1.0
    for g in [0.0, 100_000.0, 700_000.0, SURTAX_THRESHOLD_NIS,
              800_000.0, 1_500_000.0, 3_420_000.0, 10_000_000.0]:
        r = effective_cgt_rate(g)
        assert r >= prev - 1e-12
        prev = r


def test_surtax_only_bites_above_threshold():
    # Gain split so each year is under the threshold -> pure 25%, no surtax.
    assert effective_cgt_rate(SURTAX_THRESHOLD_NIS - 1.0) == pytest.approx(CGT_BASE_RATE)
    # Just above -> strictly more than base.
    assert effective_cgt_rate(SURTAX_THRESHOLD_NIS + 100_000.0) > CGT_BASE_RATE


# --- tax-bunching: faster sell-down pays more total CGT ----------------------

def test_one_year_horizon_costs_more_cgt_than_five():
    cgt1, rate1 = total_cgt_for_horizon(GAIN, 1)
    cgt5, rate5 = total_cgt_for_horizon(GAIN, 5)
    assert cgt1 > cgt5            # bunching penalty is real money
    assert rate1 > rate5          # ... driven by a higher blended rate
    # Both rates sit inside the statutory band.
    assert CGT_BASE_RATE <= rate5 <= rate1 <= CGT_MARGINAL_ABOVE_THRESHOLD


def test_total_cgt_monotonic_in_horizon():
    # More years -> never more total CGT (gain spread thinner, less surtax).
    cgts = [total_cgt_for_horizon(GAIN, h)[0] for h in (1, 2, 3, 4, 5)]
    for faster, slower in zip(cgts, cgts[1:]):
        assert slower <= faster + 1e-6


def test_total_cgt_floor_is_base_rate():
    # Spreading thin enough that each year is under the threshold -> 25% floor.
    cgt, rate = total_cgt_for_horizon(GAIN, 5)
    # 3.42M / 5 = 684k < 721,560 threshold -> exactly the base rate.
    assert rate == pytest.approx(CGT_BASE_RATE)
    assert cgt == pytest.approx(CGT_BASE_RATE * GAIN)


# --- optimizer core: structural (seed-noise robust) -------------------------

def test_per_horizon_fully_populated():
    p = _plan()
    assert [r.horizon for r in p.per_horizon] == [1, 2, 3, 4, 5]
    for r in p.per_horizon:
        assert r.total_cgt_nis > 0.0
        assert CGT_BASE_RATE <= r.eff_cgt_rate <= CGT_MARGINAL_ABOVE_THRESHOLD
        assert r.deployable_nis > 0.0
        assert r.sigma_path_desc  # non-empty descriptor
        assert r.drawdown_age is None or 44 <= r.drawdown_age <= 58


def test_optimizer_chooses_a_horizon_in_range_with_finite_age():
    p = _plan()
    assert p.chosen_horizon_years in {1, 2, 3, 4, 5}
    chosen = next(r for r in p.per_horizon if r.horizon == p.chosen_horizon_years)
    assert chosen.drawdown_age is not None
    assert isinstance(chosen.drawdown_age, int)
    assert 44 <= chosen.drawdown_age <= 58


def test_chosen_has_minimal_drawdown_age():
    p = _plan()
    feasible = [r for r in p.per_horizon if r.drawdown_age is not None]
    assert feasible, "at least one horizon must clear the bar"
    best_age = min(r.drawdown_age for r in feasible)
    chosen = next(r for r in p.per_horizon if r.horizon == p.chosen_horizon_years)
    assert chosen.drawdown_age == best_age  # picked the earliest-age horizon
    # tie-break: among horizons at the winning age, the chosen one has the lowest CGT.
    tied = [r for r in feasible if r.drawdown_age == best_age]
    assert chosen.total_cgt_nis == min(r.total_cgt_nis for r in tied)


def test_deployable_decreases_with_more_cgt():
    p = _plan()
    by_h = {r.horizon: r for r in p.per_horizon}
    # Faster sell-down -> more CGT -> strictly less deployable capital.
    assert by_h[1].total_cgt_nis > by_h[5].total_cgt_nis
    assert by_h[1].deployable_nis < by_h[5].deployable_nis
    for r in p.per_horizon:
        assert r.deployable_nis == pytest.approx(
            p.full_portfolio_nis - p.reserve_pv_nis - r.total_cgt_nis
        )


def test_assumptions_block_sourced():
    p = _plan()
    a = p.assumptions
    assert a["surtax_threshold_nis"] == SURTAX_THRESHOLD_NIS
    assert a["cgt_marginal_above_threshold"] == CGT_MARGINAL_ABOVE_THRESHOLD
    assert "surtax.md" in a["cgt_model"]
    assert a["taxable_gain_fraction"] == DEFAULT_TAXABLE_GAIN_FRACTION
    assert "real NIS" in a["gain_terms"]
