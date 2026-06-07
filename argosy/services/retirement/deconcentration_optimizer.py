"""NVDA deconcentration sell-rate OPTIMIZER — the horizon money-math core.

NVDA is ~65% of the liquid portfolio today. The plan deconcentrates it to a 13%
strategic single-name cap, selling ~₪5.7M (~₪3.42M of it taxable real gain at the
0.6 taxable-gain fraction). This module resolves the central TRADEOFF in *how
fast* to do that:

  * A **faster** sell-down glides portfolio volatility 34%→18% sooner, which
    helps the earliest-safe retirement age (sequence risk falls earlier).
  * BUT it bunches the realized capital gain into fewer tax years, pushing more
    of it through Israel's high-income surtax zone (3% `mas yesef` + 2% capital-
    source levy above ₪721,560/yr of capital income → ~30% marginal vs the 25%
    base). A higher blended CGT permanently removes more deployable capital.

The optimizer sweeps the sell-down horizon H ∈ {1..5} years and, for each H,
runs the SAME canonical dual-track Monte-Carlo machinery the rest of /plan binds
to (``retirement_plan`` helpers) on the H-specific deployable capital and
H-specific σ-glidepath, then picks the H that MINIMIZES the typical-regime
drawdown age (tie-break: lower total CGT — keep more capital for the same age).

``optimize_deconcentration_core`` is the pure, DB-free core (hand inputs → plan);
``optimize_deconcentration`` is the DB adapter that resolves the inputs first.
Mirrors ``retirement_plan.py``'s pure-core + DB-adapter split.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date

from argosy.services.cashflow_projection import (
    HouseholdState,
    PensionState,
)
from argosy.services.retirement.retirement_plan import (
    RetirementAssumptions,
    _reserve_pv,
    _run_mc,
)
from argosy.services.retirement.scenario_mc import (
    DEFAULT_NVDA_CAP_PCT,
    _calibrated_sigma,
    _gather_inputs,
    _horizon_years_to_95,
    _sigma_glidepath,
)


# --- Israeli capital-gains tax model (auditable; sourced, not magic). ---------
# Source: domain_knowledge/tax/israel/{surtax,capital_gains}.md — PwC Israel
# Individual Tax Summary + Barnea/CWS client updates, verified 2026-06-02.
#
# Base statutory CGT on the real gain for individual residents (post-2003
# acquisitions). The taxable-gain fraction itself (how much of a SALE is gain)
# is handled separately via NVDA_TAXABLE_GAIN_FRACTION; this rate applies to
# the gain.
CGT_BASE_RATE = 0.25
# `mas yesef` high-income general surtax: +3% on the portion of the year's income
# above the threshold. For a high earner already past the threshold on labour
# income, the entire year's capital gain sits in this zone; modelled here against
# the gain itself (conservative — never understates the bunching penalty).
SURTAX_GENERAL_RATE = 0.03
# Additional capital-source surtax (effective tax year 2025+): +2% on the portion
# of capital-source income above the same threshold.
SURTAX_CAPITAL_SOURCE_RATE = 0.02
# 2026 threshold, NIS/year (₪60,130/month). Wage-indexed; re-verify each January.
SURTAX_THRESHOLD_NIS = 721_560.0
# Combined marginal CGT above the threshold = 25% + 3% + 2% = 30%.
CGT_MARGINAL_ABOVE_THRESHOLD = (
    CGT_BASE_RATE + SURTAX_GENERAL_RATE + SURTAX_CAPITAL_SOURCE_RATE
)
# Safe real rate for discounting the multi-year tax stream to present value —
# future-year CGT is a deferred liability, so charging it all at t=0 over-
# penalizes slower horizons (codex money-math review 2026-06-07).
TAX_DISCOUNT_REAL = 0.02
# Fraction of the NVDA SALE that is taxable gain. NVDA is RSU-sourced (basis =
# vest-date FMV) and has run up hard, so most of the sale is embedded gain —
# 0.8 (codex review: 0.6 is a portfolio-wide blend, too low for this lot).
NVDA_TAXABLE_GAIN_FRACTION = 0.8

DEFAULT_HORIZONS: tuple[int, ...] = (1, 2, 3, 4, 5)


def effective_cgt_rate(annual_taxable_gain_nis: float) -> float:
    """Blended effective Israeli CGT rate on a SINGLE calendar year's realized
    taxable capital gain (in NIS, real terms — the statutory base is the real
    gain; cost basis is CPI-indexed, see capital_gains.md).

    For a high earner already past the threshold on labour income, the 3% `mas
    yesef` general surtax applies to the WHOLE capital gain (25% + 3% = 28%); only
    the 2% capital-source levy (2025+) is gated to the slice ABOVE the threshold,
    taking the rate to 30% there. So spreading a fixed total gain across MORE
    years only shrinks the 2% layer (the 3% is unavoidable) → a smaller bunching
    penalty than a naive threshold-gated model suggests (codex review 2026-06-07).

    Returns a rate in [0.28, 0.30]. A non-positive gain returns the base rate
    (no tax is actually due, but the *rate* is the base — the caller multiplies
    by the gain).
    """
    gain = float(annual_taxable_gain_nis)
    if gain <= 0.0:
        return CGT_BASE_RATE
    above = max(0.0, gain - SURTAX_THRESHOLD_NIS)
    tax = (
        CGT_BASE_RATE * gain
        + SURTAX_GENERAL_RATE * gain          # 3% mas-yesef on the WHOLE gain
        + SURTAX_CAPITAL_SOURCE_RATE * above  # 2% only above the threshold
    )
    return tax / gain


def total_cgt_for_horizon(total_taxable_gain_nis: float, horizon_years: int) -> tuple[float, float]:
    """Total CGT (NIS) and blended effective rate for realizing
    ``total_taxable_gain_nis`` evenly across ``horizon_years`` calendar years.

    Equal annual tranches: per-year gain = total / H, taxed at
    ``effective_cgt_rate(per-year)``. Because the surtax bites only on the slice
    above the threshold, fewer years ⇒ a higher blended rate ⇒ more total tax.
    """
    h = max(1, int(horizon_years))
    gain = max(0.0, float(total_taxable_gain_nis))
    per_year = gain / h
    rate = effective_cgt_rate(per_year)
    annual_tax = rate * per_year
    # PV of the tax stream paid across years 0..H-1 at a safe real discount rate.
    # Future-year tax is a deferred liability; charging it all at t=0 would over-
    # penalize slower horizons (codex review). H=1 → no discount (paid now).
    total = sum(annual_tax / ((1.0 + TAX_DISCOUNT_REAL) ** i) for i in range(h))
    return total, rate


@dataclass(frozen=True)
class HorizonOutcome:
    horizon: int
    total_cgt_nis: float
    eff_cgt_rate: float
    drawdown_age: int | None
    deployable_nis: float
    sigma_path_desc: str


@dataclass(frozen=True)
class DeconcentrationPlan:
    chosen_horizon_years: int | None
    per_horizon: list[HorizonOutcome]
    full_portfolio_nis: float
    reserve_pv_nis: float
    total_taxable_gain_nis: float
    nvda_current_pct: float | None
    nvda_cap_pct: float | None
    sell_nis: float
    sigma_current: float
    assumptions: dict


def optimize_deconcentration_core(
    *,
    household: HouseholdState,
    pensions: PensionState,
    full_portfolio_nis: float,
    reserve_pv_nis: float,
    total_taxable_gain_nis: float,
    sell_nis: float,
    nvda_current_pct: float | None,
    nvda_cap_pct: float | None,
    spend_central_nis: float,
    bl_monthly_nis: float,
    bl_source: str,
    annuity_tax_rate: float,
    sigma_current: float,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    target_p_solvent: float = 0.90,
    assumptions: RetirementAssumptions | None = None,
    today: date | None = None,
) -> DeconcentrationPlan:
    """Pure, DB-free core. Caller resolves the NVDA sell / taxable-gain figures,
    the full portfolio, the PV reserve, the central spend, the BL stipend, the
    annuity tax, and the calibrated σ; this sweeps the sell-down horizons.

    For each horizon H:
      (a) total CGT = Σ over H equal annual tranches of
          ``effective_cgt_rate(gain/H) × (gain/H)``  (tax-bunching penalty);
      (b) σ glides from ``sigma_current`` to 18% over H years (taper_years=H);
      (c) deployable = full_portfolio − reserve_PV − CGT(H);
      (d) drawdown age = earliest retire age whose typical-regime MC clears
          ``target_p_solvent`` to age 95 (same frontier logic as
          ``canonical_feasible_dual_track``), on (b)+(c).
    Pick the H with the LOWEST drawdown age; tie-break on lower total CGT.
    """
    a = assumptions or RetirementAssumptions()
    if target_p_solvent and target_p_solvent != a.bar_drawdown:
        a = replace(a, bar_drawdown=target_p_solvent)

    current_age = household.current_age_years
    years = _horizon_years_to_95(current_age)
    months = max(1, min(years, 60)) * 12
    start = max(int(math.ceil(current_age)), 1)

    def drawdown_age_for(*, deployable: float, taper_years: int) -> int | None:
        """One typical-regime frontier sweep — the earliest age clearing the
        drawdown bar to 95, on the H-specific σ-glide + H-specific deployable."""
        glide = _sigma_glidepath(
            months=months, current_age=current_age, retirement_age=float(start),
            sigma_hi=sigma_current, sigma_lo=a.sigma_diversified,
            taper_years=max(1, taper_years),
        )
        hh = replace(household, monthly_expenses_nis=spend_central_nis / 12.0,
                     portfolio_value_nis=deployable)
        for ra in range(start, a.max_age + 1):
            mc = _run_mc(
                hh=hh, pensions=pensions, retire_age=ra, years=years, months=months,
                mu_real=a.mu_real_typical, sigma_path=glide, a=a,
                bl_monthly=bl_monthly_nis, annuity_tax=annuity_tax_rate,
                current_age=current_age, today=today,
            )
            p95 = max(0.0, min(1.0, 1.0 - mc.p_failure_before_age_95))
            if p95 >= a.bar_drawdown:
                return ra
        return None

    rows: list[HorizonOutcome] = []
    for h in horizons:
        cgt, rate = total_cgt_for_horizon(total_taxable_gain_nis, h)
        deployable = max(0.0, full_portfolio_nis - reserve_pv_nis - cgt)
        age = drawdown_age_for(deployable=deployable, taper_years=h)
        rows.append(HorizonOutcome(
            horizon=int(h),
            total_cgt_nis=cgt,
            eff_cgt_rate=rate,
            drawdown_age=age,
            deployable_nis=deployable,
            sigma_path_desc=(
                f"σ {sigma_current:.0%}→{a.sigma_diversified:.0%} over {h}y"
            ),
        ))

    # Choose: lowest finite drawdown age; tie-break on lower total CGT (keep more
    # capital for the same retirement age). None ages sort last.
    feasible = [r for r in rows if r.drawdown_age is not None]
    chosen: int | None = None
    if feasible:
        best = min(feasible, key=lambda r: (r.drawdown_age, r.total_cgt_nis))
        chosen = best.horizon

    return DeconcentrationPlan(
        chosen_horizon_years=chosen,
        per_horizon=rows,
        full_portfolio_nis=full_portfolio_nis,
        reserve_pv_nis=reserve_pv_nis,
        total_taxable_gain_nis=total_taxable_gain_nis,
        nvda_current_pct=nvda_current_pct,
        nvda_cap_pct=nvda_cap_pct,
        sell_nis=sell_nis,
        sigma_current=sigma_current,
        assumptions={
            "objective": "minimize typical-regime drawdown age; tie-break lower total CGT",
            "horizons_years": list(horizons),
            "target_p_solvent": a.bar_drawdown,
            "cgt_base_rate": CGT_BASE_RATE,
            "surtax_general_rate": SURTAX_GENERAL_RATE,
            "surtax_capital_source_rate": SURTAX_CAPITAL_SOURCE_RATE,
            "surtax_threshold_nis": SURTAX_THRESHOLD_NIS,
            "cgt_marginal_above_threshold": CGT_MARGINAL_ABOVE_THRESHOLD,
            "taxable_gain_fraction": NVDA_TAXABLE_GAIN_FRACTION,
            "gain_terms": "real NIS gain (statutory base is the CPI-indexed real gain)",
            "cgt_model": (
                "per calendar year: 25% on the gain + 3% mas yesef + 2% capital-"
                "source levy on the portion above ₪721,560; blended over H equal "
                "annual tranches. Source: domain_knowledge/tax/israel/surtax.md "
                "(PwC Israel)."
            ),
            "sigma_diversified": a.sigma_diversified,
            "sigma_current_calibrated": sigma_current,
            "mu_real_typical": a.mu_real_typical,
            "withdrawal_tax": a.withdrawal_tax,
            "inflation": a.inflation,
            "bl_monthly_nis": bl_monthly_nis,
            "bl_source": bl_source,
            "annuity_tax_rate": annuity_tax_rate,
            "spend_central_nis": spend_central_nis,
            "n_paths": a.n_paths,
            "reserve_pv_note": "PV of finite-liability reserve at a safe real rate over avg horizon",
            "source": "deconcentration_optimizer.optimize_deconcentration_core",
        },
    )


def _resolve_nvda_sell(session, user_id: str, net_worth_nis: float) -> tuple[float, float | None, float | None]:
    """Resolve the NVDA over-cap SELL amount (NIS) the deconcentration realizes,
    plus the current / cap concentration percentages, from the same plan-numeric
    resolver ``_nvda_deconcentration_haircut`` reads. Returns (sell_nis, nvda_pct,
    cap_pct). sell_nis is 0 (and pcts None) when NVDA is already at/under the cap
    or the inputs are unavailable — mirrors the canonical haircut's defensiveness.
    """
    try:
        from sqlalchemy import desc, select

        from argosy.services.plan_numeric_resolver import resolve_plan_numbers
        from argosy.state.models import PlanVersion

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
            return 0.0, None, None
        cap_pct = cap_pct if cap_pct is not None else DEFAULT_NVDA_CAP_PCT
        sell_fraction = max(0.0, nvda_pct - cap_pct)
        return sell_fraction * net_worth_nis, nvda_pct, cap_pct
    except Exception:  # noqa: BLE001
        return 0.0, None, None


def optimize_deconcentration(
    *,
    session,
    user_id: str,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    target_p_solvent: float = 0.90,
    assumptions: RetirementAssumptions | None = None,
    today: date | None = None,
) -> DeconcentrationPlan:
    """DB adapter: resolve the NVDA sell / taxable-gain, the full portfolio, the
    PV reserve, the central spend split, the BL stipend, the annuity tax, and the
    calibrated σ, then run the pure optimizer core.

    The deployable basis per horizon is full_portfolio − PV_reserve − CGT(H),
    parallel to ``retirement_plan.build_retirement_plan`` except the CGT is now
    horizon-dependent (the very quantity being optimized) instead of the fixed
    base-rate haircut.
    """
    from argosy.services.retirement.retirement_plan import _split_spend

    a = assumptions or RetirementAssumptions()
    g = _gather_inputs(session, user_id, today)
    sigma_hi = _calibrated_sigma(session, user_id)
    full_portfolio = g.household.portfolio_value_nis
    reserve_pv = _reserve_pv(g.reserve_nis, a.reserve_discount_real, a.reserve_avg_liability_years)
    sell_nis, nvda_pct, cap_pct = _resolve_nvda_sell(session, user_id, full_portfolio)
    total_taxable_gain = sell_nis * NVDA_TAXABLE_GAIN_FRACTION
    spend_central, _spend_stress = _split_spend(session, user_id)

    return optimize_deconcentration_core(
        household=g.household, pensions=g.pensions,
        full_portfolio_nis=full_portfolio, reserve_pv_nis=reserve_pv,
        total_taxable_gain_nis=total_taxable_gain, sell_nis=sell_nis,
        nvda_current_pct=nvda_pct, nvda_cap_pct=cap_pct,
        spend_central_nis=spend_central, bl_monthly_nis=g.bl_monthly_nis,
        bl_source=g.bl_source, annuity_tax_rate=g.annuity_tax_rate,
        sigma_current=sigma_hi, horizons=horizons, target_p_solvent=target_p_solvent,
        assumptions=a, today=today,
    )


__all__ = [
    "CGT_BASE_RATE",
    "SURTAX_GENERAL_RATE",
    "SURTAX_CAPITAL_SOURCE_RATE",
    "SURTAX_THRESHOLD_NIS",
    "CGT_MARGINAL_ABOVE_THRESHOLD",
    "DEFAULT_HORIZONS",
    "HorizonOutcome",
    "DeconcentrationPlan",
    "effective_cgt_rate",
    "total_cgt_for_horizon",
    "optimize_deconcentration_core",
    "optimize_deconcentration",
]
