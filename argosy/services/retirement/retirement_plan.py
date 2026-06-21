"""Dual-track retirement plan — the optimizer-facing core.

Supersedes the single optimistic "earliest feasible age" with a transparent,
auditable plan that shows the **retire-age <-> estate-left-to-kids** tradeoff
under two tracks, recomputed live from holdings x BOI FX (no magic numbers):

  * **Drawdown-to-95** — earliest age the base Monte Carlo clears ``bar_drawdown``
    (default 90%) solvency to 95. "Retire ASAP; spend principal if needed."
  * **Capital-preservation** — earliest age at which the WORST-10% path still
    leaves real terminal wealth >= today's real deployable principal (a p10
    bequest floor, not a coin-flip median). "Live off it forever; leave the
    principal to the kids even in a bad market."

Both run on the SAME deconcentrated, reserve-netted basis so they reconcile, and
across three market regimes (bull / typical / bear). The result also carries the
per-age estate frontier (median + worst-10% bequest), the spend level that makes
retire-now safe, a stress-spend sensitivity, and an FX-stress what-if band.

Assumptions are the post-audit corrected set (codex assumption audit + code
review 2026-06-05): 5.0% real central return (4.5% conservative case), 10%
interim withdrawal tax (basis-aware drag, not the full 15% lifetime), the
finite-liability reserve held at the PV of its scheduled liabilities discounted
at a SAFE real rate (earmarked near-term money isn't invested at equity risk),
and a permanent-spend basis split into CENTRAL (real ongoing needs) and STRESS
(adds discretionary home upgrades). Late-life healthcare/LTC is modeled inside
the MC as a time-varying expense phase (see ``_mc_spend_split`` /
``apply_expense_phases``), not as a flat allowance in this basis. Estates are read
at age 95 (not the end of the >95 horizon). Every figure carries a source; nothing
is fabricated.

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


# --- Corrected assumption set (auditable; post codex audit + code review). ----
@dataclass(frozen=True)
class RetirementAssumptions:
    mu_real_typical: float = 0.050      # central; the fi_methodology DOCUMENTED return
    mu_real_bull: float = 0.060
    mu_real_conservative: float = 0.045  # labeled conservative case (not the default)
    withdrawal_tax: float = 0.10        # legacy flat-override fallback only — the
    #                                     MC now uses the age-aware effective curve
    #                                     (tax_curve.effective_withdrawal_tax_at_age,
    #                                     T3.4); this applies only if a caller forces
    #                                     apply_age_aware_tax=False.
    bear_shock_pct: float = 0.25
    bear_decade_real: float = 0.030
    bear_decade_years: int = 10
    sigma_diversified: float = SIGMA_DIVERSIFIED
    deconcentration_taper_years: int = 3
    # PV of the finite-liability reserve: discount the raw reserve over an
    # average liability horizon at a SAFE real rate (earmarked near-term money is
    # held conservatively, NOT at the equity return). ~2% ≈ recent 5y TIPS real.
    reserve_avg_liability_years: float = 5.0
    reserve_discount_real: float = 0.02
    bar_drawdown: float = 0.90
    bar_preservation: float = 0.99
    fx_stress_pcts: tuple[float, ...] = (0.0, 0.10, 0.20)
    inflation: float = DEFAULT_INFLATION_ANNUAL
    n_paths: int = 2000
    seed: int = 42
    max_age: int = 67


@dataclass(frozen=True)
class FrontierPoint:
    retire_age: int
    p_solvent_95: float
    median_estate_nis: float
    median_estate_real_nis: float        # deflated to today's purchasing power, at age 95
    worst10_estate_nis: float
    worst10_estate_real_nis: float
    principal_preserved: bool            # worst-10% real terminal >= real deployable (p10 floor)


@dataclass(frozen=True)
class ScenarioTrack:
    name: str                            # bull | typical | bear
    label: str
    mu_real: float
    drawdown_age: int | None             # earliest clearing bar_drawdown
    drawdown_p: float | None
    preservation_age: int | None         # earliest the worst-10% path preserves real principal
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
    stress_drawdown_age: int | None      # typical regime at the STRESS spend (sensitivity)
    stress_preservation_age: int | None
    spend_to_retire_now_nis: float | None
    fx_stress_band: list[tuple[float, int | None]]  # (fx_adverse_pct, drawdown_age @ typical)
    assumptions: dict


def _reserve_pv(reserve_nis: float, discount_real: float, avg_years: float) -> float:
    """PV of the finite-liability reserve. The liabilities (education ~10y,
    mortgage runoff, near-term weddings) are paid over time, so the capital that
    must be earmarked TODAY is their PV, not the full nominal sum. Discounted at
    a SAFE real rate (earmarked money is held conservatively, not at equity
    risk). Approximate until the actual liability schedule is in intake."""
    return reserve_nis / ((1.0 + discount_real) ** max(0.0, avg_years))


def _is_home_upgrade(c) -> bool:
    return "HOME_UPGRADE" in (c.source or "").upper()


def _is_flat_healthcare_ramp(c) -> bool:
    """The flat permanent healthcare allowance (planning_parameter:HEALTHCARE_RAMP)
    is SUPERSEDED in the solvency MC by the time-varying phase curve
    (phase_expenses.phase_expense_factor_series, applied via apply_expense_phases):
    keeping both would double-count late-life healthcare in every ruin path. The
    FI perpetuity (fi_methodology) KEEPS the allowance until M1 reconciles the two
    derivations. (codex H3 verdict 2026-06-08: decision A.)"""
    return "HEALTHCARE_RAMP" in (c.source or "").upper()


def _mc_spend_split(perm_components: list) -> tuple[float, float]:
    """The MC spend basis from the permanent FI components.

    CENTRAL = real ongoing needs (tracked living ex-mortgage + car cadence),
    EXCLUDING both the discretionary home-upgrade cadence and the flat
    healthcare-ramp allowance. STRESS adds back the home-upgrade cadence but
    still excludes the flat healthcare ramp (the phase curve carries late-life
    healthcare in BOTH the central and stress MC paths). Pure + DB-free for
    unit testing."""
    central = sum(
        c.annual_nis for c in perm_components
        if not _is_home_upgrade(c) and not _is_flat_healthcare_ramp(c)
    )
    stress = sum(
        c.annual_nis for c in perm_components if not _is_flat_healthcare_ramp(c)
    )
    return float(central), float(stress)


def _split_spend(session, user_id: str) -> tuple[float, float]:
    """The MC's central/stress spend basis, derived from the sourced FI
    components — never a hardcoded constant. See ``_mc_spend_split`` for the
    component selection (the flat healthcare ramp is excluded here because the
    solvency MC models late-life healthcare via the time-varying phase curve)."""
    from argosy.services.fi_methodology import compute_fi_target

    fi = compute_fi_target(session, user_id=user_id)
    if fi is None or not fi.permanent_annual_spend_nis:
        raise ValueError("retirement_plan needs an FI spend basis — refusing to fabricate one.")
    perm = [c for c in fi.components if c.kind == "permanent"]
    central, stress = _mc_spend_split(perm)
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


@dataclass(frozen=True)
class CanonicalBasis:
    """The ONE reserve-netted + CGT-haircut deployable capital, calibrated σ, and
    central/stress spend that the dual-track plan, the /retirement ruin hero, and
    the scenario grid all bind to — so the three surfaces reconcile on identical
    capital / risk / spend (H8/H9). Pure resolution; no Monte Carlo."""
    household: HouseholdState
    pensions: PensionState
    deployable_nis: float          # full_portfolio − reserve_pv − cgt_haircut
    full_portfolio_nis: float
    cgt_haircut_nis: float
    reserve_raw_nis: float
    reserve_pv_nis: float
    sigma_hi: float                # calibrated current σ (NVDA-concentrated today)
    spend_central_nis: float
    spend_stress_nis: float
    bl_monthly_nis: float
    bl_source: str
    annuity_tax_rate: float


def resolve_canonical_basis(
    session, user_id: str, *,
    assumptions: RetirementAssumptions | None = None,
    today: date | None = None,
) -> CanonicalBasis:
    """Resolve the single deconcentrated, reserve-netted, CGT-haircut basis used
    across every retirement surface. Centralizing this (vs re-deriving it per
    surface) is what guarantees the dual-track headline, the ruin hero, and the
    scenario grid reconcile (codex H8/H9).

    Memoized per (session, user_id, today, reserve-assumptions): the basis is
    ``mu_real``-INDEPENDENT (only the YAML-heavy holdings/FI/σ/CGT resolution,
    none of which read the return regime), so the /portfolio 3-scenario sweep —
    which re-invokes this once per bear/conservative/typical varying only
    ``mu_real_typical`` — recomputed the same ~2s basis three times. The cache is
    held on ``session.info`` so it lives and dies with the request-scoped DB
    session; the key includes the only two assumption fields that DO feed the
    basis (``reserve_discount_real`` / ``reserve_avg_liability_years``) so a
    caller varying those still recomputes. ``mu_real``/``n_paths``/``bar_*`` etc.
    are deliberately NOT in the key — they do not affect any basis field."""
    a = assumptions or RetirementAssumptions()
    cache_key = (
        "argosy.retirement.canonical_basis",
        user_id,
        today,
        a.reserve_discount_real,
        a.reserve_avg_liability_years,
    )
    cache = None
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        cache = info.setdefault("_argosy_canonical_basis_cache", {})
        if cache_key in cache:
            return cache[cache_key]

    basis = _resolve_canonical_basis_uncached(session, user_id, a, today)
    if cache is not None:
        cache[cache_key] = basis
    return basis


def _resolve_canonical_basis_uncached(
    session, user_id: str, a: RetirementAssumptions, today: date | None
) -> CanonicalBasis:
    g = _gather_inputs(session, user_id, today)
    sigma_hi = _calibrated_sigma(session, user_id)
    haircut = _nvda_deconcentration_haircut(
        session, user_id, g.household.portfolio_value_nis
    )
    reserve_pv = _reserve_pv(
        g.reserve_nis, a.reserve_discount_real, a.reserve_avg_liability_years
    )
    deployable = max(0.0, g.household.portfolio_value_nis - reserve_pv - haircut)
    spend_central, spend_stress = _split_spend(session, user_id)
    return CanonicalBasis(
        household=g.household, pensions=g.pensions, deployable_nis=deployable,
        full_portfolio_nis=g.household.portfolio_value_nis, cgt_haircut_nis=haircut,
        reserve_raw_nis=g.reserve_nis, reserve_pv_nis=reserve_pv, sigma_hi=sigma_hi,
        spend_central_nis=spend_central, spend_stress_nis=spend_stress,
        bl_monthly_nis=g.bl_monthly_nis, bl_source=g.bl_source,
        annuity_tax_rate=g.annuity_tax_rate,
    )


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
        # mu_real (and the bear-decade real in mu_path) are COMPOUND/geometric
        # real-return assumptions (5% real per Vanguard/BNY convention), so the
        # MC must not re-apply the -sigma^2/2 variance drag (H1). Treating them
        # as arithmetic understated the median path and biased the earliest-safe
        # age too late. regime_switch_mc stays arithmetic (its regime means are
        # arithmetic).
        mu_nominal_basis="geometric",
        # H3: shape the per-tick spend by the documented life-stage phases
        # (empty-nest dip, post-65 healthcare ramp, late-life LTC tail). The flat
        # healthcare allowance is already excluded from the spend basis upstream
        # (_mc_spend_split) so this does not double-count.
        apply_expense_phases=True,
        inflation_annual=a.inflation, n_paths=a.n_paths, seed=a.seed, today=today,
        # T3.4: retire the flat-10% withdrawal_tax shortcut — the MC now grosses
        # up the net draw with the age-aware EFFECTIVE curve (15% pre-67 CGT on
        # the gain fraction / 12% post-67 pension). withdrawal_tax stays only as
        # the override-flat fallback below.
        tax_rate=a.withdrawal_tax, apply_age_aware_tax=True,
        bl_annuity_monthly_nis=bl_monthly, annuity_tax_rate=annuity_tax,
    )


def _at_age(series, target_age: float):
    """The series point nearest ``target_age`` — the MC horizon runs PAST 95, so
    series[-1] (~age 97) overstates the age-95 estate (codex review #1)."""
    return min(series, key=lambda p: abs(p.age_years - target_age))


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
            pt95 = _at_age(mc.series, 95.0)
            med, p10 = pt95.portfolio_value_p50_nis, pt95.portfolio_value_p10_nis
            med_real = real_deflate(med, 95.0)
            p10_real = real_deflate(p10, 95.0)
            rows.append(FrontierPoint(
                retire_age=ra, p_solvent_95=p95,
                median_estate_nis=med, median_estate_real_nis=med_real,
                worst10_estate_nis=p10, worst10_estate_real_nis=p10_real,
                # bequest floor: even the worst-10% path leaves >= today's real principal
                principal_preserved=p10_real >= deployable_nis,
            ))
        return rows

    def ages_from(fr: list[FrontierPoint]) -> tuple[int | None, float | None, int | None, float | None]:
        draw = next((p for p in fr if p.p_solvent_95 >= a.bar_drawdown), None)
        pres = next((p for p in fr if p.principal_preserved and p.p_solvent_95 >= a.bar_preservation), None)
        return (draw.retire_age if draw else None, draw.p_solvent_95 if draw else None,
                pres.retire_age if pres else None, pres.p_solvent_95 if pres else None)

    specs = (
        ("typical", "Typical (5.0% real)", a.mu_real_typical, False, 0.0),
        ("bull", "Bull (6.0% real)", a.mu_real_bull, False, 0.0),
        ("bear", "Bear (-25% shock + low first decade)", a.mu_real_typical, True, a.bear_shock_pct),
    )
    tracks: list[ScenarioTrack] = []
    for name, label, mu_real, bear, shock in specs:
        fr = frontier_for(mu_real, bear=bear, shock=shock, spend=spend_central_nis, portfolio=deployable_nis)
        da, dp, pa, pp = ages_from(fr)
        tracks.append(ScenarioTrack(
            name=name, label=label, mu_real=mu_real,
            drawdown_age=da, drawdown_p=dp, preservation_age=pa, preservation_p=pp, frontier=fr,
        ))

    # Stress-spend sensitivity (typical regime at the higher STRESS spend).
    stress_fr = frontier_for(a.mu_real_typical, bear=False, shock=0.0,
                             spend=spend_stress_nis, portfolio=deployable_nis)
    s_da, _, s_pa, _ = ages_from(stress_fr)

    spend_now = _solve_spend_to_retire_now(
        household=household, pensions=pensions, a=a, glide=glide, years=years, months=months,
        start=start, bl_monthly=bl_monthly_nis, annuity_tax=annuity_tax_rate,
        current_age=current_age, portfolio=deployable_nis, today=today)

    # FX what-if: a stronger shekel cuts the NIS value of the (USD) assets BEFORE
    # the NIS-denominated reserve + CGT are netted (codex review #5).
    fx_band: list[tuple[float, int | None]] = []
    for hit in a.fx_stress_pcts:
        stressed_deployable = max(0.0, full_portfolio_nis * (1.0 - hit) - cgt_haircut_nis - reserve_pv_nis)
        fr = frontier_for(a.mu_real_typical, bear=False, shock=0.0,
                          spend=spend_central_nis, portfolio=stressed_deployable)
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
        stress_drawdown_age=s_da,
        stress_preservation_age=s_pa,
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
            "reserve_discount_real": a.reserve_discount_real,
            "bar_drawdown": a.bar_drawdown,
            "bar_preservation": a.bar_preservation,
            "preservation_test": "worst-10% real terminal wealth at 95 >= today's real deployable principal",
            "inflation": a.inflation,
            "bl_monthly_nis": bl_monthly_nis,
            "bl_source": bl_source,
            "annuity_tax_rate": annuity_tax_rate,
            "n_paths": a.n_paths,
            "spend_central_source": "fi_methodology permanent components incl. healthcare ramp (ex home-upgrade)",
            "spend_stress_source": "fi_methodology all permanent components (adds discretionary home upgrades)",
            "reserve_pv_note": "PV of finite-liability reserve at a safe real rate over avg horizon; approximate pending liability-schedule intake",
            "estate_read_at_age": 95,
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
    cb = resolve_canonical_basis(session, user_id, assumptions=a, today=today)
    return compute_retirement_plan(
        household=cb.household, pensions=cb.pensions, deployable_nis=cb.deployable_nis,
        spend_central_nis=cb.spend_central_nis, spend_stress_nis=cb.spend_stress_nis,
        bl_monthly_nis=cb.bl_monthly_nis, bl_source=cb.bl_source,
        annuity_tax_rate=cb.annuity_tax_rate, sigma_current=cb.sigma_hi,
        full_portfolio_nis=cb.full_portfolio_nis, cgt_haircut_nis=cb.cgt_haircut_nis,
        reserve_raw_nis=cb.reserve_raw_nis, reserve_pv_nis=cb.reserve_pv_nis,
        assumptions=a, today=today,
    )


def canonical_feasible_dual_track(
    *,
    session,
    user_id: str,
    target_p_solvent: float = 0.90,
    operational_target_age: float | None = None,
    assumptions: RetirementAssumptions | None = None,
    today: date | None = None,
):
    """Lightweight canonical age for the /plan headline: the TYPICAL-regime
    DRAWDOWN age (earliest clearing ``target_p_solvent`` to 95) under the
    corrected dual-track assumptions, with the capital-preservation age carried
    in ``basis``. ONE frontier sweep (fast — comparable to the old single-age
    canonical), not the full dual-track build. Returns a
    ``scenario_mc.FeasibleAgeResult`` so the existing headline/anchor code
    consumes it unchanged but now off the HONEST numbers (not the optimistic
    sigma-flat / no-CGT 49)."""
    from argosy.services.cashflow_projection import ANNUITY_AGE, LUMP_PENSION_AGE
    from argosy.services.retirement.scenario_mc import FeasibleAgeResult

    a = assumptions or RetirementAssumptions()
    if target_p_solvent and target_p_solvent != a.bar_drawdown:
        a = replace(a, bar_drawdown=target_p_solvent)

    # Single canonical basis (reserve-netted + CGT-haircut deployable, calibrated
    # σ, central spend) — the SAME one the ruin hero + scenario grid bind to.
    g = resolve_canonical_basis(session, user_id, assumptions=a, today=today)
    sigma_hi = g.sigma_hi
    reserve_pv = g.reserve_pv_nis
    deployable = g.deployable_nis
    spend_central = g.spend_central_nis

    current_age = g.household.current_age_years
    years = _horizon_years_to_95(current_age)
    months = max(1, min(years, 60)) * 12
    start = max(int(math.ceil(current_age)), 1)
    glide = _sigma_glidepath(
        months=months, current_age=current_age, retirement_age=float(start),
        sigma_hi=sigma_hi, sigma_lo=a.sigma_diversified,
        taper_years=a.deconcentration_taper_years,
    )
    hh = replace(g.household, monthly_expenses_nis=spend_central / 12.0, portfolio_value_nis=deployable)

    drawdown_age = pres_age = None
    drawdown_p = pres_p = None
    for ra in range(start, a.max_age + 1):
        mc = _run_mc(hh=hh, pensions=g.pensions, retire_age=ra, years=years, months=months,
                     mu_real=a.mu_real_typical, sigma_path=glide, a=a, bl_monthly=g.bl_monthly_nis,
                     annuity_tax=g.annuity_tax_rate, current_age=current_age, today=today)
        p95 = max(0.0, min(1.0, 1.0 - mc.p_failure_before_age_95))
        pt95 = _at_age(mc.series, 95.0)
        p10_real = pt95.portfolio_value_p10_nis / ((1.0 + a.inflation) ** (95.0 - current_age))
        if drawdown_age is None and p95 >= a.bar_drawdown:
            drawdown_age, drawdown_p = float(ra), p95
        if pres_age is None and p10_real >= deployable and p95 >= a.bar_preservation:
            pres_age, pres_p = float(ra), p95
        if drawdown_age is not None and pres_age is not None:
            break

    op_target = operational_target_age if operational_target_age is not None else 49.0
    return FeasibleAgeResult(
        earliest_feasible_age=drawdown_age,
        p_solvent_at_age=drawdown_p,
        target_p_solvent=a.bar_drawdown,
        operational_target_age=float(op_target),
        statutory_lump_age=int(LUMP_PENSION_AGE),
        statutory_annuity_age=int(ANNUITY_AGE),
        current_age=current_age,
        reserve_netted_nis=reserve_pv,
        basis={
            "method": "dual-track typical drawdown (corrected assumptions; sigma-glide + CGT + PV reserve)",
            "preservation_age": pres_age,
            "preservation_p": pres_p,
            "preservation_test": "worst-10% real terminal at 95 >= today's real deployable principal",
            "spend_central_nis": spend_central,
            "deployable_nis": deployable,
            "mu_real": a.mu_real_typical,
            "withdrawal_tax": a.withdrawal_tax,
            "reserve_pv_nis": reserve_pv,
            "source": "retirement_plan.canonical_feasible_dual_track",
        },
    )


def plan_series(
    *,
    session,
    user_id: str,
    retire_age: float,
    regime: str = "typical",
    assumptions: RetirementAssumptions | None = None,
    today: date | None = None,
):
    """Full per-tick Monte Carlo series on the dual-track PLAN basis
    (deconcentrated NVDA, σ-glide 34→18%, reserve-netted at PV, 5% real / 10%
    interim tax) for a SELECTED retire age + market regime. Feeds the
    portfolio-bands + cashflow-coverage charts so they reconcile with the
    headline ages instead of the stale "do nothing" config. Returns a
    ``cashflow_projection.MonteCarloProjection``."""
    a = assumptions or RetirementAssumptions()
    g = _gather_inputs(session, user_id, today)
    sigma_hi = _calibrated_sigma(session, user_id)
    haircut = _nvda_deconcentration_haircut(session, user_id, g.household.portfolio_value_nis)
    reserve_pv = _reserve_pv(g.reserve_nis, a.reserve_discount_real, a.reserve_avg_liability_years)
    deployable = max(0.0, g.household.portfolio_value_nis - reserve_pv - haircut)
    spend_central, _stress = _split_spend(session, user_id)
    current_age = g.household.current_age_years
    years = _horizon_years_to_95(current_age)
    months = max(1, min(years, 60)) * 12
    start = max(int(math.ceil(current_age)), 1)
    glide = _sigma_glidepath(
        months=months, current_age=current_age, retirement_age=float(start),
        sigma_hi=sigma_hi, sigma_lo=a.sigma_diversified,
        taper_years=a.deconcentration_taper_years,
    )
    bear = regime == "bear"
    mu_real = a.mu_real_bull if regime == "bull" else a.mu_real_typical
    shock = a.bear_shock_pct if bear else 0.0
    ra = max(start, int(round(retire_age)))
    hh = replace(g.household, monthly_expenses_nis=spend_central / 12.0, portfolio_value_nis=deployable)
    return _run_mc(
        hh=hh, pensions=g.pensions, retire_age=ra, years=years, months=months,
        mu_real=mu_real, sigma_path=glide, a=a, bl_monthly=g.bl_monthly_nis,
        annuity_tax=g.annuity_tax_rate, shock=shock, bear=bear,
        current_age=current_age, today=today,
    )


__all__ = [
    "RetirementAssumptions",
    "FrontierPoint",
    "ScenarioTrack",
    "RetirementPlan",
    "compute_retirement_plan",
    "build_retirement_plan",
    "canonical_feasible_dual_track",
    "plan_series",
]
