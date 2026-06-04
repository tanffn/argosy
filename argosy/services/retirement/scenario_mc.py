"""Retirement decision MC — scenario grid + sensitivities (codex review 2026-06-04).

The /retirement readiness verdict is built from a *scenario* table, not from
±1σ value bands (codex Q4: "Do not call ±1σ value bands bear/base/bull. Those
are uncertainty bands around one path, not scenarios. For retirement, the bear
case must stress the first decade because sequence risk is the load-bearing
risk").

This module runs, all on the SAME basis so the numbers reconcile:

  * **Base** — 4.5% real return, no shock. The decision-central case.
  * **Bull** — 5.5% real, no shock.
  * **Bear** — an immediate −25% hit to the liquid portfolio at retirement,
    then 3.0% real for retirement years 1-10 (sequence-risk decade), then 4.5%
    real thereafter.
  * **μ-grid sensitivity** — 4.0 / 4.5 / 5.0 / 5.5 real, no shock, so the
    reader sees how the headline moves with the return assumption.
  * **T12 sensitivity** — the base scenario re-run at the current ₪-T12 burn
    instead of the permanent-equivalent basis.
  * **Fat-tail stress** — the 3-regime Markov engine at the same basis + BL, a
    secondary readout that surfaces clustered-crash downside the lognormal
    engine can't.

Every run uses:
  * **Spend basis = the permanent-equivalent spend** the FI target was sized on
    (``fi_methodology.permanent_annual_spend_nis``, ~₪311.6k), NOT the favorable
    current T12 burn (codex Q2).
  * **Bituach Leumi income credited** from age 67, netted against spend like the
    private pension annuity (codex Q1 #5).
  * **Gain-fraction tax** (effective ≈ 15% = 25% CGT × 0.6 taxable fraction),
    matching the regime engine — not the full-shortfall gross-up (codex Q1 #4).

The lognormal engine carries the scenario grid (it is μ-responsive and well
tested); the regime engine is kept as the fat-tail readout, fed the same basis
so the two surfaces are comparable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date

import numpy as np

from argosy.services.cashflow_projection import (
    DEFAULT_INFLATION_ANNUAL,
    DEFAULT_SIGMA_ANNUAL,
    DEFAULT_TAX_RATE,
    DEFAULT_TAXABLE_GAIN_FRACTION,
    HouseholdState,
    PensionState,
    extract_household_state,
    extract_pension_state,
    project_monte_carlo,
)
from argosy.services.retirement.regime_switch_mc import simulate_regime_switch


# --- Documented scenario parameters (auditable, not LLM guesses). -----------
SCENARIO_MU_REAL: dict[str, float] = {"base": 0.045, "bull": 0.055}
BEAR_SHOCK_PCT = 0.25
BEAR_ACCUM_REAL = 0.045      # accumulation: no reason to assume bad luck PRE-retirement
BEAR_DECADE_REAL = 0.03      # retirement years 1-10 sequence-risk decade
BEAR_RECOVERY_REAL = 0.045   # codex range 4.0-4.5; use the top (anti-over-conservatism)
BEAR_DECADE_YEARS = 10
MU_GRID_REAL: list[float] = [0.040, 0.045, 0.050, 0.055]

# Effective withdrawal tax — gross up only the taxable-gain fraction of each
# sale, matching the regime engine (codex Q1 #4). Flat across ages.
EFFECTIVE_WITHDRAWAL_TAX = DEFAULT_TAX_RATE * DEFAULT_TAXABLE_GAIN_FRACTION


@dataclass(frozen=True)
class ScenarioOutcome:
    name: str           # "bear" | "base" | "bull"
    label: str
    mu_real_pct: float
    mu_nominal_pct: float
    initial_shock_pct: float
    p_solvent_75: float
    p_solvent_85: float
    p_solvent_95: float


@dataclass(frozen=True)
class GridPoint:
    mu_real_pct: float
    mu_nominal_pct: float
    p_solvent_95: float


@dataclass(frozen=True)
class ScenarioGrid:
    spend_basis_annual_nis: float
    spend_basis_source: str
    spend_t12_annual_nis: float
    bl_monthly_nis: float
    bl_source: str
    annuity_tax_rate: float
    annuity_tax_source: str
    inflation_annual: float
    sigma_annual: float
    retirement_age: float
    current_age: float
    horizon_years: int
    n_paths: int
    scenarios: list[ScenarioOutcome]
    mu_grid: list[GridPoint]
    fat_tail_p_solvent_95: float
    t12_sensitivity_p_solvent_95: float
    assumptions: dict


def _horizon_years_to_95(current_age: float) -> int:
    """Years needed so the path reaches age 95 with a margin tick."""
    return max(1, math.ceil(95.0 - current_age) + 1)


def _build_bear_mu_nominal_path(
    *, months: int, current_age: float, retirement_age: float, inflation: float,
) -> np.ndarray:
    """Per-month NOMINAL drift for the bear scenario.

    Central return during accumulation, a low-return decade for the first 10
    retirement years (sequence-risk window), then recovery. The −25% shock is
    applied separately by the engine at the retirement crossing.
    """
    path = np.empty(months, dtype=np.float64)
    for i in range(months):
        age_at = current_age + (i + 1) / 12.0
        if age_at < retirement_age:
            real = BEAR_ACCUM_REAL
        elif age_at < retirement_age + BEAR_DECADE_YEARS:
            real = BEAR_DECADE_REAL
        else:
            real = BEAR_RECOVERY_REAL
        path[i] = real + inflation
    return path


def _p_solvent(mc) -> tuple[float, float, float]:
    return (
        max(0.0, min(1.0, 1.0 - mc.p_failure_before_age_75)),
        max(0.0, min(1.0, 1.0 - mc.p_failure_before_age_85)),
        max(0.0, min(1.0, 1.0 - mc.p_failure_before_age_95)),
    )


def simulate_scenarios(
    *,
    household: HouseholdState,
    pensions: PensionState,
    retirement_age: float,
    spend_basis_annual_nis: float,
    spend_t12_annual_nis: float,
    bl_monthly_nis: float,
    inflation_annual: float = DEFAULT_INFLATION_ANNUAL,
    sigma_annual: float = DEFAULT_SIGMA_ANNUAL,
    n_paths: int = 2000,
    seed: int | None = None,
    today: date | None = None,
    annuity_tax_rate: float = 0.0,
    spend_basis_source: str = "fi_methodology.permanent_annual_spend_nis",
    bl_source: str = "bituach_leumi.estimate_bl_stipend.monthly_nis",
    annuity_tax_source: str = "tax_engine.effective_pension_annuity_tax",
) -> ScenarioGrid:
    """Pure scenario-grid core. No DB — caller supplies the resolved inputs.

    The same RNG ``seed`` is reused across scenarios so they are controlled
    comparisons (identical random draws, differing only in the assumption set).
    """
    today = today or date.today()
    current_age = household.current_age_years
    years = _horizon_years_to_95(current_age)
    months = max(1, min(years, 60)) * 12

    hh_basis = replace(household, monthly_expenses_nis=spend_basis_annual_nis / 12.0)
    hh_t12 = replace(household, monthly_expenses_nis=spend_t12_annual_nis / 12.0)

    common = dict(
        retirement_age=retirement_age,
        years=years,
        sigma_annual=sigma_annual,
        inflation_annual=inflation_annual,
        n_paths=n_paths,
        seed=seed,
        today=today,
        tax_rate=EFFECTIVE_WITHDRAWAL_TAX,
        apply_age_aware_tax=False,
        bl_annuity_monthly_nis=bl_monthly_nis,
        annuity_tax_rate=annuity_tax_rate,
    )

    def _run(hh, mu_nominal, *, shock=0.0, mu_path=None):
        return project_monte_carlo(
            household=hh,
            pensions=pensions,
            mu_nominal_annual=mu_nominal,
            initial_shock_pct=shock,
            mu_nominal_path=mu_path,
            **common,
        )

    scenarios: list[ScenarioOutcome] = []

    # Base + bull — flat real drift, no shock.
    for name, label in (("base", "Base (central)"), ("bull", "Bull")):
        mu_real = SCENARIO_MU_REAL[name]
        mu_nom = mu_real + inflation_annual
        s75, s85, s95 = _p_solvent(_run(hh_basis, mu_nom))
        scenarios.append(ScenarioOutcome(
            name=name, label=label, mu_real_pct=mu_real, mu_nominal_pct=mu_nom,
            initial_shock_pct=0.0, p_solvent_75=s75, p_solvent_85=s85, p_solvent_95=s95,
        ))

    # Bear — −25% shock at retirement + low-return decade.
    bear_path = _build_bear_mu_nominal_path(
        months=months, current_age=current_age,
        retirement_age=retirement_age, inflation=inflation_annual,
    )
    b75, b85, b95 = _p_solvent(_run(
        hh_basis, BEAR_ACCUM_REAL + inflation_annual,
        shock=BEAR_SHOCK_PCT, mu_path=bear_path,
    ))
    scenarios.append(ScenarioOutcome(
        name="bear",
        label=f"Bear (−{BEAR_SHOCK_PCT:.0%} shock + {BEAR_DECADE_REAL:.0%} real yrs 1-{BEAR_DECADE_YEARS})",
        mu_real_pct=BEAR_DECADE_REAL, mu_nominal_pct=BEAR_DECADE_REAL + inflation_annual,
        initial_shock_pct=BEAR_SHOCK_PCT,
        p_solvent_75=b75, p_solvent_85=b85, p_solvent_95=b95,
    ))

    # μ-grid sensitivity — flat, no shock.
    mu_grid: list[GridPoint] = []
    for mu_real in MU_GRID_REAL:
        mu_nom = mu_real + inflation_annual
        _, _, g95 = _p_solvent(_run(hh_basis, mu_nom))
        mu_grid.append(GridPoint(mu_real_pct=mu_real, mu_nominal_pct=mu_nom, p_solvent_95=g95))

    # T12 sensitivity — base scenario at the current burn.
    _, _, t12_95 = _p_solvent(_run(hh_t12, SCENARIO_MU_REAL["base"] + inflation_annual))

    # Fat-tail stress — regime-switch engine, same basis + BL.
    rs = simulate_regime_switch(
        household=hh_basis,
        pensions=pensions,
        retirement_age=retirement_age,
        years=years,
        inflation_annual=inflation_annual,
        n_paths=n_paths,
        seed=seed,
        today=today,
        bl_annuity_monthly_nis=bl_monthly_nis,
        annuity_tax_rate=annuity_tax_rate,
    )
    fat_tail_95 = max(0.0, min(1.0, 1.0 - rs.p_failure_before_age.get(95, 0.0)))

    return ScenarioGrid(
        spend_basis_annual_nis=spend_basis_annual_nis,
        spend_basis_source=spend_basis_source,
        spend_t12_annual_nis=spend_t12_annual_nis,
        bl_monthly_nis=bl_monthly_nis,
        bl_source=bl_source,
        annuity_tax_rate=annuity_tax_rate,
        annuity_tax_source=annuity_tax_source,
        inflation_annual=inflation_annual,
        sigma_annual=sigma_annual,
        retirement_age=retirement_age,
        current_age=current_age,
        horizon_years=years,
        n_paths=n_paths,
        scenarios=scenarios,
        mu_grid=mu_grid,
        fat_tail_p_solvent_95=fat_tail_95,
        t12_sensitivity_p_solvent_95=t12_95,
        assumptions={
            "spend_basis_source": spend_basis_source,
            "bl_source": bl_source,
            "annuity_tax_rate": annuity_tax_rate,
            "annuity_tax_source": annuity_tax_source,
            "effective_withdrawal_tax": EFFECTIVE_WITHDRAWAL_TAX,
            "bear_shock_pct": BEAR_SHOCK_PCT,
            "bear_decade_real": BEAR_DECADE_REAL,
            "bear_decade_years": BEAR_DECADE_YEARS,
            "bear_recovery_real": BEAR_RECOVERY_REAL,
            "sigma_annual": sigma_annual,
            "inflation_annual": inflation_annual,
            "model_notes": (
                "Lognormal MC carries base/bull/bear + μ-grid; regime-switch "
                "engine carries the fat-tail readout. Spend = permanent-"
                "equivalent basis; BL credited from age 67; tax = 25% CGT on "
                "the 0.6 taxable-gain fraction (≈15% effective). Bear = −25% "
                "shock at retirement + low-return first decade (sequence risk)."
            ),
        },
    )


def run_retirement_scenarios(
    *,
    user_id: str,
    session,
    retirement_age: float = 49.0,
    n_paths: int = 2000,
    seed: int | None = None,
    today: date | None = None,
) -> ScenarioGrid:
    """DB adapter: resolve the spend basis + BL stipend, then run the core.

    Spend basis = ``fi_methodology.permanent_annual_spend_nis`` (the number the
    FI target was sized on). BL = the central old-age stipend estimate for the
    user (Ariel only; spouse stipend is a documented pending uplift — crediting
    one stipend is the conservative choice). Raises ``ValueError`` when the FI
    basis cannot be sourced — never substitutes a fabricated constant.
    """
    from argosy.services.fi_methodology import compute_fi_target
    from argosy.services.retirement.bituach_leumi import estimate_bl_stipend
    from argosy.services.retirement.tax_engine import effective_pension_annuity_tax

    household = extract_household_state(session, user_id=user_id, today=today)
    pensions = extract_pension_state(session, user_id=user_id)

    fi = compute_fi_target(session, user_id=user_id)
    if fi is None or not fi.permanent_annual_spend_nis:
        raise ValueError(
            "scenario MC needs a permanent-equivalent spend basis "
            "(fi_methodology returned no baseline) — refusing to fabricate one."
        )
    spend_basis_annual = float(fi.permanent_annual_spend_nis)

    spend_t12_annual = household.monthly_expenses_nis * 12.0
    if spend_t12_annual <= 0:
        spend_t12_annual = float(fi.baseline_annual_nis)

    age = household.current_age_years
    age_int = max(0, int(round(age)))
    # BL insured-year convention matches the BL card: full-time work from ~22.
    history_years = max(0, age_int - 22)
    bl_monthly = 0.0
    bl_src = "bituach_leumi.estimate_bl_stipend.monthly_nis (Ariel; no spouse uplift)"
    try:
        est = estimate_bl_stipend(
            current_age=age_int,
            contribution_history_years=history_years,
            spouse_eligible=False,
            user_id=user_id,
            session=session,
        )
        bl_monthly = float(est.monthly_nis.value or 0.0)
    except Exception:  # noqa: BLE001 — BL is an income credit; absence is conservative
        bl_monthly = 0.0
        bl_src = "bituach_leumi unavailable — BL income not credited (conservative)"

    # Effective income tax on the post-67 private pension annuity, sourced from
    # the ITA exemption phasing + household marginal rate (codex review
    # 2026-06-04). Use the claim-year (age 67) so the exemption schedule is
    # applied at the right point.
    base_year = (today or date.today()).year
    claim_year = base_year + max(0, int(round(67.0 - age)))
    annuity_tax = effective_pension_annuity_tax(
        user_id=user_id, session=session, year=claim_year,
    )

    return simulate_scenarios(
        household=household,
        pensions=pensions,
        retirement_age=retirement_age,
        spend_basis_annual_nis=spend_basis_annual,
        spend_t12_annual_nis=spend_t12_annual,
        bl_monthly_nis=bl_monthly,
        annuity_tax_rate=annuity_tax,
        n_paths=n_paths,
        seed=seed,
        today=today,
        spend_basis_source=(
            "fi_methodology.permanent_annual_spend_nis"
        ),
        bl_source=bl_src,
        annuity_tax_source=(
            f"tax_engine.effective_pension_annuity_tax (claim year {claim_year})"
        ),
    )


__all__ = [
    "ScenarioOutcome",
    "GridPoint",
    "ScenarioGrid",
    "simulate_scenarios",
    "run_retirement_scenarios",
]
