"""Regime-switching Monte Carlo for retirement projections.

Closes HIGH #11 from the 2026-05-28 SDD review. The prior MC used a single
lognormal Gaussian process — too smooth. A 2008-style cluster or 2022-
stagflation period can't appear because the underlying process has no
regime structure.

This module implements a Markov regime-switching model with three states:
  - calm:      μ=0.10, σ=0.13  (bull-market normal, ~70% of months)
  - turbulent: μ=0.04, σ=0.22  (correction/recession-cusp, ~25% of months)
  - crisis:    μ=-0.30, σ=0.45 (2008-style crash, ~5% of months)

Transition matrix (calibrated to post-1970 S&P 500 monthly regimes per
common academic mappings; tweak via the ``transition_matrix`` arg):
            to_calm  to_turb  to_crisis
  from_calm  0.94    0.05     0.01
  from_turb  0.20    0.70     0.10
  from_cris  0.05    0.40     0.55

Per-tick output identical to ``project_monte_carlo`` so downstream code
(ruin_probability) can swap engines transparently.

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 3 HIGH #11.
"""
import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np
from sqlalchemy.orm import Session

from argosy.services.cashflow_projection import (
    ANNUITY_AGE,
    LUMP_PENSION_AGE,
    DEFAULT_INFLATION_ANNUAL,
    DEFAULT_LIFESTYLE_DRIFT_ANNUAL,
    DEFAULT_MEKADEM,
    DEFAULT_TAX_RATE,
    DEFAULT_TAXABLE_GAIN_FRACTION,
    HouseholdState,
    PensionState,
)
from argosy.services.retirement.citations import ValueWithRationale


RegimeId = Literal["calm", "turbulent", "crisis"]

# Per-regime annualized (μ, σ) — calibrated to post-1970 S&P 500 monthly
# regimes per Hamilton-Markov-switching style mappings.
DEFAULT_REGIME_PARAMS: dict[RegimeId, tuple[float, float]] = {
    "calm": (0.10, 0.13),
    "turbulent": (0.04, 0.22),
    "crisis": (-0.30, 0.45),
}

# Markov transition probabilities (rows: from-state in order calm/turb/crisis).
DEFAULT_TRANSITION_MATRIX = np.array([
    [0.94, 0.05, 0.01],  # from calm
    [0.20, 0.70, 0.10],  # from turbulent
    [0.05, 0.40, 0.55],  # from crisis
])


@dataclass(frozen=True)
class RegimeSwitchResult:
    """Per-tick percentile + fraction_solvent + age-stratified failure probs.

    Mirrors ``MonteCarloProjection`` so ruin_probability can consume it
    via a simple adapter.
    """
    portfolio_p10: np.ndarray  # shape (months+1,)
    portfolio_p25: np.ndarray
    portfolio_p50: np.ndarray
    portfolio_p75: np.ndarray
    portfolio_p90: np.ndarray
    fraction_solvent_per_month: np.ndarray  # shape (months+1,)
    p_failure_before_age: dict[int, float]  # {75: 0.12, 85: 0.18, 95: 0.22}
    regime_occupancy: dict[RegimeId, float]  # avg fraction of time in each regime
    months: int
    n_paths: int


def _simulate_regimes(
    *,
    n_paths: int,
    months: int,
    transition_matrix: np.ndarray,
    rng: np.random.Generator,
    initial_regime: int = 0,  # 0 = calm
) -> np.ndarray:
    """Generate (n_paths, months) array of regime states (0/1/2).

    Markov chain advances one step per month.
    """
    regimes = np.zeros((n_paths, months), dtype=np.int8)
    regimes[:, 0] = initial_regime
    # Pre-sample uniforms for the Markov draws
    u = rng.random((n_paths, months - 1))
    # Cumulative transition for vectorized sampling
    cum = np.cumsum(transition_matrix, axis=1)
    for t in range(1, months):
        prior = regimes[:, t - 1]
        # For each path, next regime = first column where cumulative >= u
        thresholds = cum[prior]  # shape (n_paths, 3)
        # cmp: u < thresholds → True; argmax gives first True column
        regimes[:, t] = np.argmax(u[:, t - 1, None] < thresholds, axis=1)
    return regimes


def simulate_regime_switch(
    *,
    household: HouseholdState,
    pensions: PensionState,
    retirement_age: float,
    years: int = 40,
    inflation_annual: float = DEFAULT_INFLATION_ANNUAL,
    mekadem: float = DEFAULT_MEKADEM,
    tax_rate: float = DEFAULT_TAX_RATE,
    lifestyle_drift_annual: float = DEFAULT_LIFESTYLE_DRIFT_ANNUAL,
    n_paths: int = 2000,
    regime_params: dict[RegimeId, tuple[float, float]] | None = None,
    transition_matrix: np.ndarray | None = None,
    seed: int | None = None,
    today: date | None = None,
) -> RegimeSwitchResult:
    """Run a regime-switching MC and return the result.

    Mostly mirrors ``project_monte_carlo`` but generates per-path monthly
    log-returns conditional on the current regime.
    """
    today = today or date.today()
    # Cap raised 50→60 so a horizon that reaches age 95 from the mid-40s
    # (~51 years) isn't truncated (codex MC review 2026-06-04).
    months = max(1, min(years, 60)) * 12
    if regime_params is None:
        regime_params = DEFAULT_REGIME_PARAMS
    if transition_matrix is None:
        transition_matrix = DEFAULT_TRANSITION_MATRIX

    # Per-regime monthly log-drift + log-std
    regimes_order: list[RegimeId] = ["calm", "turbulent", "crisis"]
    drifts = np.array([
        regime_params[r][0] / 12.0 - (regime_params[r][1] ** 2) / 24.0
        for r in regimes_order
    ])
    stds = np.array([
        regime_params[r][1] / math.sqrt(12.0) for r in regimes_order
    ])

    rng = np.random.default_rng(seed)
    regimes = _simulate_regimes(
        n_paths=n_paths,
        months=months,
        transition_matrix=transition_matrix,
        rng=rng,
    )
    # log_returns[i, t] = drifts[regime] + std[regime] * standard_normal
    z = rng.normal(size=(n_paths, months))
    log_returns = drifts[regimes] + stds[regimes] * z

    portfolio = np.full(n_paths, household.portfolio_value_nis, dtype=np.float64)
    pensia_bal = np.full(n_paths, pensions.kupat_pensia_balance_nis, dtype=np.float64)
    exec_bal = np.full(n_paths, pensions.executive_insurance_balance_nis, dtype=np.float64)
    hisht_bal = np.full(n_paths, pensions.keren_hishtalmut_balance_nis, dtype=np.float64)
    gemel_bal = np.full(n_paths, pensions.kupat_gemel_balance_nis, dtype=np.float64)
    failed = np.zeros(n_paths, dtype=bool)
    lump_unlocked = False
    annuity_locked = False
    annuity_real_monthly = 0.0
    annuity_lock_t = 0
    expense_growth = inflation_annual + lifestyle_drift_annual
    real_return = sum(
        regime_params[r][0] for r in regimes_order
    ) / len(regimes_order) - inflation_annual
    real_monthly = 1.0 + real_return / 12.0

    portfolio_history = np.zeros((months + 1, n_paths), dtype=np.float64)
    portfolio_history[0] = portfolio.copy()
    solvent_history = np.ones((months + 1, n_paths), dtype=bool)

    for t in range(1, months + 1):
        age_t = household.current_age_years + t / 12.0

        portfolio[~failed] = portfolio[~failed] * np.exp(log_returns[~failed, t - 1])

        if not annuity_locked:
            contrib_pensia = (
                pensions.kupat_pensia_contribution_monthly_nis
                if age_t < retirement_age else 0.0
            )
            pensia_bal = pensia_bal * real_monthly + contrib_pensia
            exec_bal = exec_bal * real_monthly
        if not lump_unlocked:
            contrib_hisht = (
                pensions.keren_hishtalmut_contribution_monthly_nis
                if age_t < retirement_age else 0.0
            )
            hisht_bal = hisht_bal * real_monthly + contrib_hisht
            gemel_bal = gemel_bal * real_monthly

        if age_t >= LUMP_PENSION_AGE and not lump_unlocked:
            lump_total = hisht_bal[0] + gemel_bal[0]
            portfolio[~failed] = portfolio[~failed] + lump_total
            hisht_bal[:] = 0.0
            gemel_bal[:] = 0.0
            lump_unlocked = True

        if age_t >= ANNUITY_AGE and not annuity_locked:
            annuity_real_monthly = (pensia_bal[0] + exec_bal[0]) / max(mekadem, 1.0)
            annuity_locked = True
            annuity_lock_t = t

        if annuity_locked:
            annuity_nominal_t = annuity_real_monthly * (
                (1.0 + inflation_annual) ** ((t - annuity_lock_t) / 12.0)
            )
        else:
            annuity_nominal_t = 0.0

        if age_t < retirement_age:
            # WORKING YEARS: income funds living expenses — the portfolio is
            # NOT drawn down; it ACCUMULATES the household's monthly savings.
            # The prior code withdrew full expenses every month from the
            # current age, phantom-depleting the portfolio for the pre-
            # retirement years and badly understating solvency (codex MC
            # review 2026-06-04; mirrors the lognormal MC's working-years
            # gate in cashflow_projection.py).
            portfolio[~failed] = portfolio[~failed] + household.monthly_savings_nis
        else:
            expenses_t = household.monthly_expenses_nis * (
                (1.0 + expense_growth) ** (t / 12.0)
            )
            shortfall = max(0.0, expenses_t - annuity_nominal_t)
            # Gross up only the TAXABLE-GAIN portion of the sale, not the whole
            # withdrawal (codex MC review: the flat 1/(1-tax) assumed 100% of
            # each sale is taxable real gain — no cost basis / cash / dividend
            # return-of-capital). effective ≈ 25% × 0.6 = 15%.
            effective_tax = tax_rate * DEFAULT_TAXABLE_GAIN_FRACTION
            denom = max(1.0 - effective_tax, 0.01)
            withdraw_pretax = shortfall / denom
            portfolio[~failed] = portfolio[~failed] - withdraw_pretax
            new_failures = (~failed) & (portfolio <= 0)
            failed = failed | new_failures
            portfolio = np.maximum(portfolio, 0.0)

        portfolio_history[t] = portfolio
        solvent_history[t] = ~failed

    # Percentile bands over time
    p10 = np.percentile(portfolio_history, 10, axis=1)
    p25 = np.percentile(portfolio_history, 25, axis=1)
    p50 = np.percentile(portfolio_history, 50, axis=1)
    p75 = np.percentile(portfolio_history, 75, axis=1)
    p90 = np.percentile(portfolio_history, 90, axis=1)
    fraction_solvent = solvent_history.mean(axis=1)

    # Age-stratified failure probs
    def _age_to_tick(target_age: float) -> int:
        delta = target_age - household.current_age_years
        return max(0, min(months, int(round(delta * 12))))

    p_failure_before_age: dict[int, float] = {}
    for age_target in (75, 85, 95):
        tick = _age_to_tick(age_target)
        p_failure_before_age[age_target] = float(1.0 - fraction_solvent[tick])

    # Average regime occupancy across paths
    occupancy: dict[RegimeId, float] = {}
    for i, r in enumerate(regimes_order):
        occupancy[r] = float((regimes == i).mean())

    return RegimeSwitchResult(
        portfolio_p10=p10,
        portfolio_p25=p25,
        portfolio_p50=p50,
        portfolio_p75=p75,
        portfolio_p90=p90,
        fraction_solvent_per_month=fraction_solvent,
        p_failure_before_age=p_failure_before_age,
        regime_occupancy=occupancy,
        months=months,
        n_paths=n_paths,
    )


def regime_summary_value(result: RegimeSwitchResult) -> ValueWithRationale:
    """Wrap the result's overall verdict-friendly headline number.

    Returns P(solvent at 95) under regime-switching dynamics.
    """
    p_solvent_95 = 1.0 - result.p_failure_before_age.get(95, 0.0)
    return ValueWithRationale(
        value=round(p_solvent_95, 4),
        unit="fraction",
        source_id="argosy_derived",
        rationale=(
            f"P(solvent at 95) under 3-regime Markov MC. Path-level regime "
            f"occupancy: calm {result.regime_occupancy['calm']:.0%}, "
            f"turbulent {result.regime_occupancy['turbulent']:.0%}, "
            f"crisis {result.regime_occupancy['crisis']:.0%}. "
            "Adds fat-tail behavior the lognormal-Gaussian engine cannot "
            "produce (clustered crashes + stagflation regimes)."
        ),
        confidence="medium",
    )
