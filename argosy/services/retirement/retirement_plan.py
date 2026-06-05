"""Dual-track retirement plan — the optimizer-facing core.

Supersedes the single optimistic "earliest feasible age" with a transparent,
auditable plan that shows the **retire-age <-> estate-left-to-kids** tradeoff
under two tracks, recomputed live from holdings x BOI FX (no magic numbers):

  * **Drawdown-to-95** — earliest age the base Monte Carlo clears ``bar_drawdown``
    (default 90%) solvency to 95. "Retire ASAP; spend principal if needed."
  * **Capital-preservation** — earliest age that clears ``bar_preservation``
    (default 99%) AND leaves median *real* terminal wealth >= the real portfolio
    at retirement. "Live off it forever; leave the principal to the kids."

Both run on the SAME deconcentrated, reserve-netted basis so they reconcile, and
across three market regimes (bull / typical / bear). The result also carries the
per-age estate frontier (median + worst-10% bequest), the spend level that makes
retire-now safe, and an FX-stress what-if band (USD assets vs NIS spend).

Assumptions are the post-audit corrected set (codex assumption audit 2026-06-05):
5.0% real central return (4.5% conservative case), 10% interim withdrawal tax
(basis-aware drag, not the full 15% lifetime), reserve held at the PV of its
scheduled liabilities (not amputated upfront), and a permanent-spend basis split
into a confidence-trimmed CENTRAL (HIGH/MEDIUM components) and a STRESS (all
components, incl. the LOW-confidence healthcare/home heuristics). Every figure
carries a source; nothing is fabricated.

``compute_retirement_plan`` is the pure, DB-free core (hand inputs → plan);
``build_retirement_plan`` is the DB adapter that resolves the inputs first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date

import numpy as np

from argosy.services.cashflow_projection import (
    DEFAULT_INFLATION_ANNUAL,
    HouseholdState,
    PensionState,
    project_monte_carlo,
)
from argosy.services.retirement.scenario_mc import (
    SIGMA_DIVERSIFIED,
    _calibrated_sigma,
    _gather_inputs,
    _horizon_years_to_95,
    _nvda_deconcentration_haircut,
    _sigma_glidepath,
)


# --- Corrected assumption set (auditable; post codex assumption audit). ------
@dataclass(frozen=True)
class RetirementAssumptions:
    mu_real_typical: float = 0.050      # central; the fi_methodology DOCUMENTED return
    mu_real_bull: float = 0.060
    mu_real_conservative: float = 0.045  # labeled conservative case (not the default)
    withdrawal_tax: float = 0.10        # interim shortcut; basis-aware schedule later
    bear_shock_pct: float = 0.25
    bear_decade_real: float = 0.030
    bear_decade_years: int = 10
    sigma_diversified: float = SIGMA_DIVERSIFIED
    deconcentration_taper_years: int = 3
    # PV of the finite-liability reserve: discount the raw reserve at the real
    # return over an average liability horizon (approximate pending mortgage-term
    # / education-schedule intake — better than amputating the full sum at t=0).
    reserve_avg_liability_years: float = 5.0
    bar_drawdown: float = 0.90
    bar_preservation: float = 0.99
    fx_stress_pcts: tuple[float, ...] = (0.0, 0.10, 0.20)
    inflation: float = DEFAULT_INFLATION_ANNUAL
    n_paths: int = 1500
    seed: int = 42
    max_age: int = 67


@dataclass(frozen=True)
class FrontierPoint:
    retire_age: int
    p_solvent_95: float
    median_estate_nis: float
    median_estate_real_nis: float        # deflated to today's purchasing power
    worst10_estate_nis: float
    principal_preserved: bool            # median real terminal >= real deployable


@dataclass(frozen=True)
class ScenarioTrack:
    name: str                            # bull | typical | bear
    label: str
    mu_real: float
    drawdown_age: int | None             # earliest clearing bar_drawdown
    drawdown_p: float | None
    preservation_age: int | None         # earliest clearing bar_preservation + principal intact
    preservation_p: float | None
    frontier: list[FrontierPoint]


@dataclass(frozen=True)
class RetirementPlan:
    current_age: float
    full_portfolio_nis: float
    cgt_haircut_nis: float
    reserve_raw_nis: float
    reserve_pv_nis: float
    deployable_nis: float
    spend_central_nis: float
    spend_stress_nis: float
    sigma_current: float
    tracks: list[ScenarioTrack]          # at spend_central
    spend_to_retire_now_nis: float | None
    fx_stress_band: list[tuple[float, int | None]]  # (fx_adverse_pct, drawdown_age @ typical)
    assumptions: dict


def _reserve_pv(reserve_nis: float, mu_real: float, avg_years: float) -> float:
    """PV of the finite-liability reserve. The liabilities (education ~10y,
    mortgage runoff, near-term weddings) are paid over time, so the capital that
    must be earmarked TODAY is their PV, not the full nominal sum. Discounted at
    the real return over an average horizon. Approximate until the actual
    liability schedule is in intake."""
    return reserve_nis / ((1.0 + mu_real) ** max(0.0, avg_years))


def _split_spend(session, user_id: str) -> tuple[float, float]:
    """CENTRAL = permanent components at HIGH/MEDIUM confidence (tracked living
    ex-mortgage + car cadence); STRESS = all permanent components (adds the
    LOW-confidence healthcare ramp + home-upgrade heuristics). Derived from the
    sourced fi components — never a hardcoded constant."""
    from argosy.services.fi_methodology import compute_fi_target

    fi = compute_fi_target(session, user_id=user_id)
    if fi is None or not fi.permanent_annual_spend_nis:
        raise ValueError("retirement_plan needs an FI spend basis — refusing to fabricate one.")
    perm = [c for c in fi.components if c.kind == "permanent"]
    central = sum(c.annual_nis for c in perm if c.confidence in ("HIGH", "MEDIUM"))
    stress = sum(c.annual_nis for c in perm)
    if central <= 0:  # defensive — never run on a zero basis
        central = stress
    return float(central), float(stress)


def _bear_mu_path(months: int, current_age: float, retire_age: int,
                  mu_real: float, a: RetirementAssumptions) -> np.ndarray:
    path = np.empty(months, dtype=np.float64)
    for i in range(months):
        age_at = current_age + (i + 1) / 12.0
        in_decade = retire_age <= age_at < retire_age + a.bear_decade_years
        path[i] = (a.bear_decade_real if in_decade else mu_real) + a.inflation
    return path


def _run_mc(*, hh: HouseholdState, pensions: PensionState, retire_age: int, years: int,
            months: int, mu_real: float, sigma_path, a: RetirementAssumptions,
            bl_monthly: float, annuity_tax: float, current_age: float,
            today: date | None, shock: float = 0.0, bear: bool = False):
    mu_path = (_bear_mu_path(months, current_age, retire_age, mu_real, a)
               if bear else None)
    return project_monte_carlo(
        household=hh, pensions=pensions, retirement_age=float(retire_age), years=years,
        mu_nominal_annual=mu_real + a.inflation, sigma_annual=a.sigma_diversified,
        sigma_nominal_path=sigma_path, mu_nominal_path=mu_path, initial_shock_pct=shock,
        inflation_annual=a.inflation, n_paths=a.n_paths, seed=a.seed, today=today,
        tax_rate=a.withdrawal_tax, apply_age_aware_tax=False,
        bl_annuity_monthly_nis=bl_monthly, annuity_tax_rate=annuity_tax,
    )


def compute_retirement_plan(
    *,
    household: HouseholdState,
    pensions: PensionState,
    deployable_nis: float,
    spend_central_nis: float,
    spend_stress_nis: float,
    bl_monthly_nis: float,
    bl_source: str,
    annuity_tax_rate: float,
    sigma_current: float,
    full_portfolio_nis: float,
    cgt_haircut_nis: float,
    reserve_raw_nis: float,
    reserve_pv_nis: float,
    assumptions: RetirementAssumptions | None = None,
    today: date | None = None,
) -> RetirementPlan:
    """Pure, DB-free core. Caller resolves the inputs (deployable capital already
    net of CGT + PV reserve; central/stress spend; BL stipend; annuity tax;
    current calibrated sigma) and this builds the dual-track plan."""
    a = assumptions or RetirementAssumptions()
    current_age = household.current_age_years
    years = _horizon_years_to_95(current_age)
    months = max(1, min(years, 60)) * 12
    start = max(int(math.ceil(current_age)), 1)
    glide = _sigma_glidepath(
        months=months, current_age=current_age, retirement_age=float(start),
        sigma_hi=sigma_current, sigma_lo=a.sigma_diversified,
        taper_years=a.deconcentration_taper_years,
    )

    def real_deflate(nominal: float, age: float) -> float:
        return nominal / ((1.0 + a.inflation) ** (age - current_age))

    def frontier_for(mu_real: float, *, bear: bool, shock: float, spend: float,
                     portfolio: float) -> list[FrontierPoint]:
        hh = replace(household, monthly_expenses_nis=spend / 12.0, portfolio_value_nis=portfolio)
        rows: list[FrontierPoint] = []
        for ra in range(start, a.max_age + 1):
            mc = _run_mc(hh=hh, pensions=pensions, retire_age=ra, years=years, months=months,
                         mu_real=mu_real, sigma_path=glide, a=a, bl_monthly=bl_monthly_nis,
                         annuity_tax=annuity_tax_rate, shock=shock, bear=bear,
                         current_age=current_age, today=today)
            p95 = max(0.0, min(1.0, 1.0 - mc.p_failure_before_age_95))
            end = mc.series[-1]
            med, p10 = end.portfolio_value_p50_nis, end.portfolio_value_p10_nis
            med_real = real_deflate(med, 95.0)
            rows.append(FrontierPoint(
                retire_age=ra, p_solvent_95=p95,
                median_estate_nis=med, median_estate_real_nis=med_real,
                worst10_estate_nis=p10, principal_preserved=med_real >= portfolio,
            ))
        return rows

    specs = (
        ("typical", "Typical (5.0% real)", a.mu_real_typical, False, 0.0),
        ("bull", "Bull (6.0% real)", a.mu_real_bull, False, 0.0),
        ("bear", "Bear (-25% shock + low first decade)", a.mu_real_typical, True, a.bear_shock_pct),
    )
    tracks: list[ScenarioTrack] = []
    for name, label, mu_real, bear, shock in specs:
        fr = frontier_for(mu_real, bear=bear, shock=shock, spend=spend_central_nis, portfolio=deployable_nis)
        draw = next((p for p in fr if p.p_solvent_95 >= a.bar_drawdown), None)
        pres = next((p for p in fr if p.p_solvent_95 >= a.bar_preservation and p.principal_preserved), None)
        tracks.append(ScenarioTrack(
            name=name, label=label, mu_real=mu_real,
            drawdown_age=draw.retire_age if draw else None,
            drawdown_p=draw.p_solvent_95 if draw else None,
            preservation_age=pres.retire_age if pres else None,
            preservation_p=pres.p_solvent_95 if pres else None,
            frontier=fr,
        ))

    spend_now = _solve_spend_to_retire_now(
        household=household, pensions=pensions, a=a, glide=glide, years=years, months=months,
        start=start, bl_monthly=bl_monthly_nis, annuity_tax=annuity_tax_rate,
        current_age=current_age, portfolio=deployable_nis, today=today)

    # FX what-if: a stronger shekel cuts the NIS value of USD assets.
    fx_band: list[tuple[float, int | None]] = []
    for hit in a.fx_stress_pcts:
        fr = frontier_for(a.mu_real_typical, bear=False, shock=0.0,
                          spend=spend_central_nis, portfolio=deployable_nis * (1.0 - hit))
        draw = next((p for p in fr if p.p_solvent_95 >= a.bar_drawdown), None)
        fx_band.append((hit, draw.retire_age if draw else None))

    return RetirementPlan(
        current_age=current_age,
        full_portfolio_nis=full_portfolio_nis,
        cgt_haircut_nis=cgt_haircut_nis,
        reserve_raw_nis=reserve_raw_nis,
        reserve_pv_nis=reserve_pv_nis,
        deployable_nis=deployable_nis,
        spend_central_nis=spend_central_nis,
        spend_stress_nis=spend_stress_nis,
        sigma_current=sigma_current,
        tracks=tracks,
        spend_to_retire_now_nis=spend_now,
        fx_stress_band=fx_band,
        assumptions={
            "mu_real_typical": a.mu_real_typical,
            "mu_real_bull": a.mu_real_bull,
            "mu_real_conservative": a.mu_real_conservative,
            "withdrawal_tax": a.withdrawal_tax,
            "sigma_diversified": a.sigma_diversified,
            "sigma_current_calibrated": sigma_current,
            "deconcentration_taper_years": a.deconcentration_taper_years,
            "reserve_avg_liability_years": a.reserve_avg_liability_years,
            "bar_drawdown": a.bar_drawdown,
            "bar_preservation": a.bar_preservation,
            "inflation": a.inflation,
            "bl_monthly_nis": bl_monthly_nis,
            "bl_source": bl_source,
            "annuity_tax_rate": annuity_tax_rate,
            "n_paths": a.n_paths,
            "spend_central_source": "fi_methodology permanent components @ HIGH/MEDIUM confidence",
            "spend_stress_source": "fi_methodology all permanent components (incl. LOW-confidence)",
            "reserve_pv_note": "PV of finite-liability reserve at real return over avg horizon; approximate pending liability-schedule intake",
            "source": "retirement_plan.compute_retirement_plan",
        },
    )


def _solve_spend_to_retire_now(
    *, household: HouseholdState, pensions: PensionState, a: RetirementAssumptions, glide,
    years: int, months: int, start: int, bl_monthly: float, annuity_tax: float,
    current_age: float, portfolio: float, today: date | None,
    lo: float = 60_000.0, hi: float = 600_000.0, iters: int = 18,
) -> float | None:
    """Max annual spend at which retiring NOW (age == start) clears the drawdown
    bar under the typical regime. Binary search; None if even ``lo`` fails."""
    def p_at(spend: float) -> float:
        hh = replace(household, monthly_expenses_nis=spend / 12.0, portfolio_value_nis=portfolio)
        mc = _run_mc(hh=hh, pensions=pensions, retire_age=start, years=years, months=months,
                     mu_real=a.mu_real_typical, sigma_path=glide, a=a, bl_monthly=bl_monthly,
                     annuity_tax=annuity_tax, current_age=current_age, today=today)
        return max(0.0, min(1.0, 1.0 - mc.p_failure_before_age_95))

    if p_at(lo) < a.bar_drawdown:
        return None
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if p_at(mid) >= a.bar_drawdown:
            lo = mid
        else:
            hi = mid
    return lo


def build_retirement_plan(
    *,
    session,
    user_id: str,
    assumptions: RetirementAssumptions | None = None,
    today: date | None = None,
) -> RetirementPlan:
    """DB adapter: resolve the deconcentrated reserve-netted deployable capital,
    the central/stress spend split, the calibrated sigma, then run the pure core."""
    a = assumptions or RetirementAssumptions()
    g = _gather_inputs(session, user_id, today)
    sigma_hi = _calibrated_sigma(session, user_id)
    haircut = _nvda_deconcentration_haircut(session, user_id, g.household.portfolio_value_nis)
    reserve_pv = _reserve_pv(g.reserve_nis, a.mu_real_typical, a.reserve_avg_liability_years)
    deployable = max(0.0, g.household.portfolio_value_nis - reserve_pv - haircut)
    spend_central, spend_stress = _split_spend(session, user_id)
    return compute_retirement_plan(
        household=g.household, pensions=g.pensions, deployable_nis=deployable,
        spend_central_nis=spend_central, spend_stress_nis=spend_stress,
        bl_monthly_nis=g.bl_monthly_nis, bl_source=g.bl_source,
        annuity_tax_rate=g.annuity_tax_rate, sigma_current=sigma_hi,
        full_portfolio_nis=g.household.portfolio_value_nis, cgt_haircut_nis=haircut,
        reserve_raw_nis=g.reserve_nis, reserve_pv_nis=reserve_pv,
        assumptions=a, today=today,
    )


__all__ = [
    "RetirementAssumptions",
    "FrontierPoint",
    "ScenarioTrack",
    "RetirementPlan",
    "compute_retirement_plan",
    "build_retirement_plan",
]
