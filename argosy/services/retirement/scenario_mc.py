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


@dataclass(frozen=True)
class _GatheredInputs:
    household: HouseholdState
    pensions: PensionState
    spend_basis_annual_nis: float
    spend_t12_annual_nis: float
    reserve_nis: float
    bl_monthly_nis: float
    bl_source: str
    annuity_tax_rate: float
    annuity_tax_source: str


def _gather_inputs(session, user_id: str, today: date | None) -> _GatheredInputs:
    """Resolve the shared scenario inputs once (spend basis, finite-liability
    reserve, BL stipend, effective annuity tax). Single source so the scenario
    grid AND the earliest-feasible-age sweep bind to identical assumptions.
    Raises ValueError when the FI spend basis can't be sourced (never fabricated).
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
    reserve_nis = float(fi.finite_liability_reserve_nis or 0.0)

    spend_t12_annual = household.monthly_expenses_nis * 12.0
    if spend_t12_annual <= 0:
        spend_t12_annual = float(fi.baseline_annual_nis)

    age = household.current_age_years
    age_int = max(0, int(round(age)))
    history_years = max(0, age_int - 22)
    bl_monthly = 0.0
    bl_src = "bituach_leumi.estimate_bl_stipend.monthly_nis (Ariel; no spouse uplift)"
    try:
        est = estimate_bl_stipend(
            current_age=age_int, contribution_history_years=history_years,
            spouse_eligible=False, user_id=user_id, session=session,
        )
        bl_monthly = float(est.monthly_nis.value or 0.0)
    except Exception:  # noqa: BLE001
        bl_monthly = 0.0
        bl_src = "bituach_leumi unavailable — BL income not credited (conservative)"

    base_year = (today or date.today()).year
    claim_year = base_year + max(0, int(round(67.0 - age)))
    annuity_tax = effective_pension_annuity_tax(
        user_id=user_id, session=session, year=claim_year,
    )

    return _GatheredInputs(
        household=household, pensions=pensions,
        spend_basis_annual_nis=spend_basis_annual, spend_t12_annual_nis=spend_t12_annual,
        reserve_nis=reserve_nis, bl_monthly_nis=bl_monthly, bl_source=bl_src,
        annuity_tax_rate=annuity_tax,
        annuity_tax_source=f"tax_engine.effective_pension_annuity_tax (claim year {claim_year})",
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
    """DB adapter: resolve the spend basis + BL stipend, then run the core."""
    g = _gather_inputs(session, user_id, today)
    return simulate_scenarios(
        household=g.household, pensions=g.pensions, retirement_age=retirement_age,
        spend_basis_annual_nis=g.spend_basis_annual_nis,
        spend_t12_annual_nis=g.spend_t12_annual_nis,
        bl_monthly_nis=g.bl_monthly_nis, annuity_tax_rate=g.annuity_tax_rate,
        n_paths=n_paths, seed=seed, today=today,
        spend_basis_source="fi_methodology.permanent_annual_spend_nis",
        bl_source=g.bl_source, annuity_tax_source=g.annuity_tax_source,
    )


@dataclass(frozen=True)
class FeasibleAgeResult:
    """The ONE canonical retirement-age answer every surface binds to.

    ``earliest_feasible_age`` is the earliest age the base-case Monte Carlo
    clears ``target_p_solvent`` at 95 with the finite-liability reserve
    earmarked (sequence-of-returns aware). This is the honest "earliest you can
    safely retire" — it supersedes the deterministic income-crossing test that
    reported the current age. The other ages are the labeled anchors so no
    surface contradicts another (age-coherence 1b).
    """
    earliest_feasible_age: float | None
    p_solvent_at_age: float | None
    target_p_solvent: float
    operational_target_age: float
    statutory_lump_age: int
    statutory_annuity_age: int
    current_age: float
    reserve_netted_nis: float
    basis: dict


def earliest_feasible_retire_age(
    *,
    session,
    user_id: str,
    target_p_solvent: float = 0.90,
    mu_real_central: float = 0.045,
    operational_target_age: float = 49.0,
    n_paths: int = 2000,
    seed: int | None = 42,
    today: date | None = None,
    max_age: int = 70,
) -> FeasibleAgeResult:
    """Sweep retirement age and return the earliest the base-case MC clears the
    solvency bar — reserve-netted, permanent-equivalent spend, BL credited,
    annuity taxed. The reserve (education/mortgage/weddings) is earmarked out of
    the portfolio because it WILL be spent, so it can't also fund the perpetuity.
    """
    from argosy.services.cashflow_projection import (
        ANNUITY_AGE,
        DEFAULT_SIGMA_ANNUAL,
        LUMP_PENSION_AGE,
    )

    g = _gather_inputs(session, user_id, today)
    infl = DEFAULT_INFLATION_ANNUAL
    current_age = g.household.current_age_years
    years = _horizon_years_to_95(current_age)
    netted_portfolio = max(0.0, g.household.portfolio_value_nis - g.reserve_nis)
    hh = replace(
        g.household,
        monthly_expenses_nis=g.spend_basis_annual_nis / 12.0,
        portfolio_value_nis=netted_portfolio,
    )

    earliest: float | None = None
    p_at: float | None = None
    start = max(int(math.ceil(current_age)), 1)
    for ra in range(start, max_age + 1):
        mc = project_monte_carlo(
            household=hh, pensions=g.pensions, retirement_age=float(ra), years=years,
            mu_nominal_annual=mu_real_central + infl, sigma_annual=DEFAULT_SIGMA_ANNUAL,
            inflation_annual=infl, n_paths=n_paths, seed=seed, today=today,
            tax_rate=EFFECTIVE_WITHDRAWAL_TAX, apply_age_aware_tax=False,
            bl_annuity_monthly_nis=g.bl_monthly_nis, annuity_tax_rate=g.annuity_tax_rate,
        )
        p95 = max(0.0, min(1.0, 1.0 - mc.p_failure_before_age_95))
        if p95 >= target_p_solvent:
            earliest, p_at = float(ra), p95
            break

    return FeasibleAgeResult(
        earliest_feasible_age=earliest,
        p_solvent_at_age=p_at,
        target_p_solvent=target_p_solvent,
        operational_target_age=operational_target_age,
        statutory_lump_age=int(LUMP_PENSION_AGE),
        statutory_annuity_age=int(ANNUITY_AGE),
        current_age=current_age,
        reserve_netted_nis=g.reserve_nis,
        basis={
            "method": "earliest age base-case MC clears the solvency bar, reserve-netted",
            "target_p_solvent": target_p_solvent,
            "mu_real_central": mu_real_central,
            "spend_basis_annual_nis": g.spend_basis_annual_nis,
            "reserve_netted_nis": g.reserve_nis,
            "portfolio_after_reserve_nis": netted_portfolio,
            "bl_monthly_nis": g.bl_monthly_nis,
            "annuity_tax_rate": g.annuity_tax_rate,
            "n_paths": n_paths,
            "source": "scenario_mc.earliest_feasible_retire_age",
        },
    )


SIGMA_DIVERSIFIED = 0.18          # post-deconcentration target σ (8-asset-class)
DECONCENTRATION_TAPER_YEARS = 3   # σ falls hi→lo over the NVDA sell-down period
DEFAULT_NVDA_CAP_PCT = 0.13       # strategic single-name ceiling (fallback)


def _calibrated_sigma(session, user_id: str) -> float:
    """Current portfolio σ from the calibrator (NVDA-concentrated ≈ 0.34).
    Falls back to the diversified target if unavailable."""
    try:
        from argosy.services.cashflow_assumptions import get_default_assumptions
        a = get_default_assumptions(session=session, user_id=user_id)
        v = float(a.sigma_annual.value)
        return v if v > 0 else SIGMA_DIVERSIFIED
    except Exception:  # noqa: BLE001
        return SIGMA_DIVERSIFIED


def _nvda_deconcentration_haircut(session, user_id: str, net_worth_nis: float) -> float:
    """The CGT cost of selling NVDA down to the strategic cap — the one-time
    capital haircut the 'deconcentrated' scenario pays to de-risk. Gain taxed on
    the taxable fraction (≈15% effective, matching the withdrawal tax). 0 when
    NVDA is already at/under the cap or the inputs are unavailable."""
    try:
        from argosy.services.plan_numeric_resolver import resolve_plan_numbers
        from argosy.state.models import PlanVersion
        from sqlalchemy import select, desc
        pv = session.execute(
            select(PlanVersion).where(
                PlanVersion.user_id == user_id, PlanVersion.role == "current"
            ).order_by(desc(PlanVersion.id)).limit(1)
        ).scalar_one_or_none()
        drun = getattr(pv, "decision_run_id", None) if pv else None
        nvda_pct = cap_pct = None
        if drun is not None:
            r = resolve_plan_numbers(session, user_id=user_id, decision_run_id=int(drun))
            cur = r.get("concentration.nvda_current_pct")
            cap = r.get("concentration.nvda_cap_pct")
            nvda_pct = float(cur.value) if (cur and cur.status == "resolved" and cur.value) else None
            cap_pct = float(cap.value) if (cap and cap.status == "resolved" and cap.value) else None
        if nvda_pct is None:
            return 0.0
        cap_pct = cap_pct if cap_pct is not None else DEFAULT_NVDA_CAP_PCT
        sell_fraction = max(0.0, nvda_pct - cap_pct)
        sell_nis = sell_fraction * net_worth_nis
        return sell_nis * EFFECTIVE_WITHDRAWAL_TAX  # gain-fraction × CGT
    except Exception:  # noqa: BLE001
        return 0.0


def _sigma_glidepath(
    *, months: int, current_age: float, retirement_age: float,
    sigma_hi: float, sigma_lo: float, taper_years: int,
) -> "object":
    """Per-month σ declining hi→lo over ``taper_years`` starting NOW (tick 0) —
    the NVDA sell-down is a present action (~3y to the cap), so volatility falls
    from the calibrated ~34% toward the diversified ~18% during the working
    years; by a mid/late-40s retirement σ is already at the floor. ``retirement_age``
    is unused for the anchor but kept for signature symmetry."""
    import numpy as np
    path = np.full(months, sigma_lo, dtype=np.float64)
    taper = max(1, taper_years * 12)
    for i in range(months):
        if i < taper:
            frac = i / taper
            path[i] = sigma_hi + (sigma_lo - sigma_hi) * frac
        else:
            path[i] = sigma_lo
    return path


def earliest_feasible_scenarios(
    *,
    session,
    user_id: str,
    target_p_solvent: float = 0.90,
    mu_real_central: float = 0.045,
    operational_target_age: float = 49.0,
    n_paths: int = 1200,
    seed: int | None = 42,
    today: date | None = None,
    max_age: int = 70,
) -> list[FeasibleAgeResult]:
    """Earliest-safe age under THREE genuinely-different, labeled scenarios
    (not the deterministic-bug 'same age'): 'as_is' keeps the NVDA-concentrated
    portfolio (flat calibrated σ); 'deconcentrated' sells NVDA to the cap (σ
    glidepath hi→18%, portfolio net of the one-time CGT haircut); 'bear' is the
    deconcentrated path plus a −25% retirement shock + a low-return first decade.
    """
    from argosy.services.cashflow_projection import DEFAULT_SIGMA_ANNUAL  # noqa: F401

    g = _gather_inputs(session, user_id, today)
    infl = DEFAULT_INFLATION_ANNUAL
    current_age = g.household.current_age_years
    years = _horizon_years_to_95(current_age)
    months = max(1, min(years, 60)) * 12
    sigma_hi = _calibrated_sigma(session, user_id)
    haircut = _nvda_deconcentration_haircut(session, user_id, g.household.portfolio_value_nis)

    base_portfolio = max(0.0, g.household.portfolio_value_nis - g.reserve_nis)
    decon_portfolio = max(0.0, base_portfolio - haircut)
    glide = _sigma_glidepath(
        months=months, current_age=current_age, retirement_age=operational_target_age,
        sigma_hi=sigma_hi, sigma_lo=SIGMA_DIVERSIFIED, taper_years=DECONCENTRATION_TAPER_YEARS,
    )

    specs = (
        ("as_is", "As-is (keep NVDA, σ≈{:.0%})".format(sigma_hi), base_portfolio,
         {"sigma_flat": sigma_hi}),
        ("deconcentrated", "Deconcentrated (sell NVDA to cap, σ→{:.0%})".format(SIGMA_DIVERSIFIED),
         decon_portfolio, {"sigma_glidepath": True, "cgt_haircut_nis": haircut}),
        ("bear", "Bear (deconcentrated + −25% shock + low decade)", decon_portfolio,
         {"sigma_glidepath": True, "cgt_haircut_nis": haircut, "shock": 0.25}),
    )

    out: list[FeasibleAgeResult] = []
    for name, label, portfolio, knobs in specs:
        hh = replace(
            g.household,
            monthly_expenses_nis=g.spend_basis_annual_nis / 12.0,
            portfolio_value_nis=portfolio,
        )
        sigma_flat = knobs.get("sigma_flat")
        sigma_path = None if sigma_flat is not None else glide
        shock = knobs.get("shock", 0.0)
        # Bear: low-return first decade overlaid on the central μ.
        mu_path = None
        if name == "bear":
            import numpy as np
            mu_path = np.empty(months, dtype=np.float64)
            for i in range(months):
                age_at = current_age + (i + 1) / 12.0
                real = (0.03 if operational_target_age <= age_at < operational_target_age + 10 else mu_real_central)
                mu_path[i] = real + infl

        earliest = p_at = None
        for ra in range(max(int(math.ceil(current_age)), 1), max_age + 1):
            mc = project_monte_carlo(
                household=replace(hh, monthly_expenses_nis=g.spend_basis_annual_nis / 12.0),
                pensions=g.pensions, retirement_age=float(ra), years=years,
                mu_nominal_annual=mu_real_central + infl,
                sigma_annual=(sigma_flat if sigma_flat is not None else SIGMA_DIVERSIFIED),
                sigma_nominal_path=sigma_path, mu_nominal_path=mu_path,
                initial_shock_pct=shock, inflation_annual=infl,
                n_paths=n_paths, seed=seed, today=today,
                tax_rate=EFFECTIVE_WITHDRAWAL_TAX, apply_age_aware_tax=False,
                bl_annuity_monthly_nis=g.bl_monthly_nis, annuity_tax_rate=g.annuity_tax_rate,
            )
            p95 = max(0.0, min(1.0, 1.0 - mc.p_failure_before_age_95))
            if p95 >= target_p_solvent:
                earliest, p_at = float(ra), p95
                break

        from argosy.services.cashflow_projection import ANNUITY_AGE, LUMP_PENSION_AGE
        out.append(FeasibleAgeResult(
            earliest_feasible_age=earliest, p_solvent_at_age=p_at,
            target_p_solvent=target_p_solvent, operational_target_age=operational_target_age,
            statutory_lump_age=int(LUMP_PENSION_AGE), statutory_annuity_age=int(ANNUITY_AGE),
            current_age=current_age, reserve_netted_nis=g.reserve_nis,
            basis={"scenario": name, "label": label, "sigma_hi": sigma_hi,
                   "sigma_lo": SIGMA_DIVERSIFIED, "portfolio_nis": portfolio,
                   "cgt_haircut_nis": knobs.get("cgt_haircut_nis", 0.0), **knobs},
        ))
    return out


__all__ = [
    "ScenarioOutcome",
    "GridPoint",
    "ScenarioGrid",
    "FeasibleAgeResult",
    "simulate_scenarios",
    "run_retirement_scenarios",
    "earliest_feasible_retire_age",
    "earliest_feasible_scenarios",
]
