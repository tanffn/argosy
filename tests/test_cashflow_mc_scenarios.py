"""Engine-level tests for the scenario MC primitives (codex MC review 2026-06-04).

These exercise the four backward-compatible additions to
``project_monte_carlo`` that the retirement scenario runner depends on:

  * ``bl_annuity_monthly_nis`` / ``bl_start_age`` — Bituach Leumi income netted
    into the retirement shortfall (codex Q1 #5: BL was never credited).
  * ``initial_shock_pct`` — a one-time portfolio hit at the retirement crossing
    (codex Q4: a genuine bear must stress the first decade / sequence risk).
  * ``mu_nominal_path`` — a per-month nominal-return path so a scenario can run
    a time-varying drift (bear = low decade then recovery).
  * horizon cap raised 50→60 months-years so age 95 is actually reachable from
    the mid-40s (codex Q1 #1: the age-95 tick was silently clamping).

All deterministic via ``seed``. No DB needed — the engine takes plain state.
"""
from datetime import date

import numpy as np

from argosy.services.cashflow_projection import (
    HouseholdState,
    PensionState,
    project_monte_carlo,
)


def _household(
    *, spend: float = 30_000.0, portfolio: float = 4_000_000.0, age: float = 45.0,
) -> HouseholdState:
    return HouseholdState(
        monthly_expenses_nis=spend,
        portfolio_value_nis=portfolio,
        fx_usd_nis=3.7,
        current_age_years=age,
        monthly_savings_nis=0.0,
    )


def _pensions() -> PensionState:
    return PensionState(
        kupat_pensia_balance_nis=800_000.0,
        kupat_pensia_contribution_monthly_nis=3_400.0,
        executive_insurance_balance_nis=750_000.0,
        keren_hishtalmut_balance_nis=380_000.0,
        keren_hishtalmut_contribution_monthly_nis=2_700.0,
        kupat_gemel_balance_nis=75_000.0,
    )


COMMON = dict(
    retirement_age=49.0,
    years=52,
    n_paths=400,
    seed=42,
    today=date(2026, 1, 1),
)

# years=52 capped at 60 → 52*12 months.
MONTHS = min(52, 60) * 12


class TestBituachLeumiCredit:
    def test_bl_income_reduces_ruin(self):
        """Crediting a BL stipend from age 67 nets against spend → fewer paths
        exhaust → lower P(failure at 95)."""
        h = _household(spend=45_000.0)
        base = project_monte_carlo(household=h, pensions=_pensions(), **COMMON)
        with_bl = project_monte_carlo(
            household=h, pensions=_pensions(),
            bl_annuity_monthly_nis=8_000.0, **COMMON,
        )
        assert with_bl.p_failure_before_age_95 < base.p_failure_before_age_95

    def test_bl_before_start_age_has_no_effect(self):
        """BL only kicks in at bl_start_age (67). A run with the stipend but a
        start age beyond the horizon must equal the no-BL run."""
        h = _household(spend=45_000.0)
        base = project_monte_carlo(household=h, pensions=_pensions(), **COMMON)
        bl_never = project_monte_carlo(
            household=h, pensions=_pensions(),
            bl_annuity_monthly_nis=8_000.0, bl_start_age=200.0, **COMMON,
        )
        assert bl_never.p_failure_before_age_95 == base.p_failure_before_age_95


class TestInitialShock:
    def test_shock_increases_ruin(self):
        """A −25% hit at retirement raises P(failure at 95) vs no shock."""
        h = _household(spend=40_000.0)
        base = project_monte_carlo(household=h, pensions=_pensions(), **COMMON)
        shocked = project_monte_carlo(
            household=h, pensions=_pensions(),
            initial_shock_pct=0.25, **COMMON,
        )
        assert shocked.p_failure_before_age_95 > base.p_failure_before_age_95

    def test_zero_shock_is_noop(self):
        h = _household(spend=40_000.0)
        base = project_monte_carlo(household=h, pensions=_pensions(), **COMMON)
        zero = project_monte_carlo(
            household=h, pensions=_pensions(),
            initial_shock_pct=0.0, **COMMON,
        )
        assert zero.p_failure_before_age_95 == base.p_failure_before_age_95


class TestMuPath:
    def test_flat_path_matches_scalar(self):
        """A constant μ path must reproduce the scalar-μ run bit-for-bit
        (the path mechanism only redistributes drift, it doesn't perturb the
        RNG stream)."""
        h = _household(spend=40_000.0)
        mu = 0.07
        path = np.full(MONTHS, mu)
        scalar = project_monte_carlo(
            household=h, pensions=_pensions(), mu_nominal_annual=mu, **COMMON,
        )
        pathed = project_monte_carlo(
            household=h, pensions=_pensions(), mu_nominal_annual=mu,
            mu_nominal_path=path, **COMMON,
        )
        assert pathed.p_failure_before_age_95 == scalar.p_failure_before_age_95
        assert (
            pathed.series[-1].portfolio_value_p50_nis
            == scalar.series[-1].portfolio_value_p50_nis
        )

    def test_low_return_decade_increases_ruin(self):
        """A bear decade (lower μ for retirement years 1-10) raises ruin vs a
        flat path at the central μ."""
        h = _household(spend=40_000.0)
        flat = np.full(MONTHS, 0.07)
        bear = flat.copy()
        # Retirement at 49 from age 45 → first retirement month = 48.
        # Years 1-10 of retirement = months 48..168 get 3% real (≈5.5% nominal).
        bear[48:168] = 0.055
        base = project_monte_carlo(
            household=h, pensions=_pensions(),
            mu_nominal_annual=0.07, mu_nominal_path=flat, **COMMON,
        )
        low = project_monte_carlo(
            household=h, pensions=_pensions(),
            mu_nominal_annual=0.07, mu_nominal_path=bear, **COMMON,
        )
        assert low.p_failure_before_age_95 > base.p_failure_before_age_95


class TestBituachLeumiNominalization:
    def test_real_bl_equal_to_real_spend_covers_every_age_after_67(self):
        """A CPI-indexed BL stipend equal to real spend must cover spend at
        EVERY age ≥ 67 — so once BL kicks in, solvency stops declining. Holds
        only if BL is inflated from t=0 (codex review BLOCKER 1); inflating
        only from the lock tick undercredits BL vs nominal-at-t expenses."""
        h = HouseholdState(
            monthly_expenses_nis=20_000.0,
            portfolio_value_nis=12_000_000.0,  # survive the 49→67 drawdown
            fx_usd_nis=3.7, current_age_years=44.0, monthly_savings_nis=0.0,
        )
        no_pension = PensionState(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)  # isolate BL
        mc = project_monte_carlo(
            household=h, pensions=no_pension, retirement_age=49.0, years=52,
            n_paths=300, seed=3, today=date(2026, 1, 1),
            bl_annuity_monthly_nis=20_000.0,  # == real monthly spend
            inflation_annual=0.03,
        )
        tick_67 = round((67.0 - 44.0) * 12)  # first BL tick
        assert mc.series[tick_67].fraction_solvent == mc.series[-1].fraction_solvent
        assert mc.series[tick_67].fraction_solvent > 0.0


class TestBearStressesPensions:
    """Codex review BLOCKER 2: the bear must stress market-exposed pension
    balances, not just the liquid portfolio."""

    def _ann(self, mc, age, current_age):
        t = round((age - current_age) * 12)
        return mc.series[t].pension_annuity_monthly_nis

    def test_shock_reduces_pension_annuity(self):
        h = _household(spend=40_000.0)
        base = project_monte_carlo(household=h, pensions=_pensions(), **COMMON)
        shocked = project_monte_carlo(
            household=h, pensions=_pensions(), initial_shock_pct=0.25, **COMMON,
        )
        assert self._ann(shocked, 70, h.current_age_years) < self._ann(base, 70, h.current_age_years)

    def test_low_return_path_reduces_pension_annuity(self):
        h = _household(spend=40_000.0)
        flat = np.full(MONTHS, 0.07)
        low = flat.copy()
        low[:300] = 0.04  # depressed returns through the pre-annuity years
        base = project_monte_carlo(
            household=h, pensions=_pensions(), mu_nominal_annual=0.07,
            mu_nominal_path=flat, **COMMON,
        )
        slow = project_monte_carlo(
            household=h, pensions=_pensions(), mu_nominal_annual=0.07,
            mu_nominal_path=low, **COMMON,
        )
        assert self._ann(slow, 70, h.current_age_years) < self._ann(base, 70, h.current_age_years)


class TestAnnuityIncomeTax:
    """Codex review 2026-06-04 re-block: the pension annuity is partly taxable
    income, not tax-free. Netting it gross is material optimism."""

    def test_taxing_the_annuity_increases_ruin(self):
        h = _household(spend=40_000.0)
        gross = project_monte_carlo(household=h, pensions=_pensions(), **COMMON)
        taxed = project_monte_carlo(
            household=h, pensions=_pensions(), annuity_tax_rate=0.155, **COMMON,
        )
        assert taxed.p_failure_before_age_95 > gross.p_failure_before_age_95

    def test_zero_annuity_tax_is_noop(self):
        h = _household(spend=40_000.0)
        gross = project_monte_carlo(household=h, pensions=_pensions(), **COMMON)
        zero = project_monte_carlo(
            household=h, pensions=_pensions(), annuity_tax_rate=0.0, **COMMON,
        )
        assert zero.p_failure_before_age_95 == gross.p_failure_before_age_95


class TestPensionAnnuityNominalization:
    def test_annuity_at_68_is_real_balance_nominalized_from_t0(self):
        """The emitted pension annuity must be the real-grown balance ÷ mekadem
        NOMINALIZED from t=0 — not frozen at its 2026 nominal level then inflated
        only from the age-67 lock (codex review 2026-06-04 re-block: that
        undercredited the annuity by the full CPI factor to 67, ~1.77×)."""
        current_age = 44.0
        mekadem = 200.0
        mu, infl = 0.07, 0.025
        real_monthly = 1.0 + (mu - infl) / 12.0
        # Contributions zeroed → the balance at lock is analytic.
        p = PensionState(
            kupat_pensia_balance_nis=800_000.0,
            kupat_pensia_contribution_monthly_nis=0.0,
            executive_insurance_balance_nis=750_000.0,
            keren_hishtalmut_balance_nis=0.0,
            keren_hishtalmut_contribution_monthly_nis=0.0,
            kupat_gemel_balance_nis=0.0,
        )
        h = HouseholdState(
            monthly_expenses_nis=20_000.0, portfolio_value_nis=10_000_000.0,
            fx_usd_nis=3.7, current_age_years=current_age, monthly_savings_nis=0.0,
        )
        mc = project_monte_carlo(
            household=h, pensions=p, retirement_age=49.0, years=52,
            mu_nominal_annual=mu, inflation_annual=infl, mekadem=mekadem,
            n_paths=50, seed=11, today=date(2026, 1, 1),
        )
        t_lock = round((67.0 - current_age) * 12)  # 276
        bal_at_lock = (800_000.0 + 750_000.0) * (real_monthly ** t_lock)
        annuity_real = bal_at_lock / mekadem
        t68 = round((68.0 - current_age) * 12)  # 288
        expected = annuity_real * ((1.0 + infl) ** (t68 / 12.0))
        assert mc.series[t68].pension_annuity_monthly_nis == _pytest_approx(expected)


def _pytest_approx(v):
    import pytest
    return pytest.approx(v, rel=1e-6)


class TestHorizonReachesAge95:
    def test_series_reaches_95_from_mid_forties(self):
        """From age ~44 the horizon must actually reach age 95 — the old 50-year
        cap ended at ~94 and the age-95 tick clamped to that (codex Q1 #1)."""
        h = _household(age=43.8)
        mc = project_monte_carlo(
            household=h, pensions=_pensions(),
            retirement_age=49.0, years=52, n_paths=50, seed=1,
            today=date(2026, 1, 1),
        )
        assert mc.series[-1].age_years >= 95.0
