"""Umbrella router for retirement-engine endpoints (Wave 0+).

Wave 0 surfaces only the sources + reference primitives. Later waves
register additional endpoints on this same ``/api/retirement/*`` prefix
without touching the cross-cutting plumbing here.

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``.
"""
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.retirement.bituach_leumi import estimate_bl_stipend
from argosy.services.retirement.citations import as_dict
from argosy.services.retirement.mekadem import (
    get_mekadem_for_fund,
    monthly_annuity_for_band,
)
from argosy.services.retirement.reference import ResolveError, resolve
from argosy.services.retirement.ruin_probability import compute_ruin_probability
from argosy.services.retirement.safety_gates import compute_safety_gates
from argosy.services.retirement.glide_path import compute_glide_path
from argosy.services.retirement.healthcare import (
    build_healthcare_curve,
    healthcare_share_of_burn,
)
from argosy.services.retirement.lifecycle_income import build_lifecycle_timeline
from argosy.services.retirement.phase_expenses import (
    build_phase_expense_curve,
    idf_service_phases,
)
from argosy.services.retirement.rebalancing import detect_rebalancing_alerts
from argosy.services.retirement.sigma_calibration import (
    calibrate_sigma_from_holdings,
)
from argosy.services.retirement.sources import load_sources
from argosy.services.retirement.stochastic_fx import (
    fx_band_at_horizon,
    simulate_stochastic_fx,
)
from argosy.services.retirement.action_engine import (
    PrioritizedAction,
    prioritize_actions,
)
from argosy.services.retirement.behavioral import (
    check_fomo_buy,
    check_panic_sell,
)
from argosy.services.retirement.decumulation import optimize_decumulation_order
from argosy.services.retirement.hishtalmut import (
    check_hishtalmut_eligibility,
    tax_on_hishtalmut_withdrawal,
)
from argosy.services.retirement.insurance_gaps import compute_insurance_gaps
from argosy.services.retirement.lump_vs_annuity import compute_lump_vs_annuity
from argosy.services.retirement.mortgage import build_mortgage_schedule
from argosy.services.retirement.multi_goal import (
    GoalConstraint,
    balance_multi_goals,
)
from argosy.services.retirement.partner_state import (
    extract_partner_state,
    household_retire_ready_age,
)
from argosy.services.retirement.real_estate import extract_real_estate_state
from argosy.services.retirement.replan_triggers import list_known_triggers
from argosy.services.retirement.severance import (
    effective_pension_for_annuity,
    extract_severance_state,
)
from argosy.services.retirement.tax_engine import TaxableCashflow, compute_tax
from argosy.services.retirement.windfall_allocator import propose_allocations
from argosy.services.retirement.windfall_detector import (
    DEFAULT_THRESHOLD_NIS,
    DEFAULT_THRESHOLD_USD,
    detect_windfall,
)
from argosy.services.retirement.withdrawal_policy import list_policies
from argosy.services.retirement_timeline import build_holistic_timeline
from argosy.services.plan_monitor import (
    check_allocation_drift,
    check_mc_regression,
    get_active_drift_flags,
    get_active_mc_regression_flags,
)

router = APIRouter(prefix="/retirement", tags=["retirement"])


class SourceDTO(BaseModel):
    id: str
    title: str
    url: str
    as_of: str
    kind: str
    notes: str = ""


class SourcesResponse(BaseModel):
    sources: dict[str, SourceDTO]


@router.get("/sources", response_model=SourcesResponse)
def get_sources() -> SourcesResponse:
    reg = load_sources()
    return SourcesResponse(
        sources={
            sid: SourceDTO(
                id=s.id,
                title=s.title,
                url=s.url,
                as_of=s.as_of,
                kind=s.kind,
                notes=s.notes,
            )
            for sid, s in reg.sources.items()
        },
    )


@router.get("/sources/{source_id}", response_model=SourceDTO)
def get_source(source_id: str) -> SourceDTO:
    reg = load_sources()
    s = reg.get(source_id)
    if s is None:
        raise HTTPException(status_code=404, detail=f"unknown source: {source_id!r}")
    return SourceDTO(
        id=s.id,
        title=s.title,
        url=s.url,
        as_of=s.as_of,
        kind=s.kind,
        notes=s.notes,
    )


@router.get("/reference/{key}")
def get_reference(
    key: str,
    user_id: str,
    db: Session = Depends(get_db),
) -> dict:
    try:
        v = resolve(key, user_id=user_id, session=db)
    except ResolveError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return as_dict(v)


# Wave 1 — mekadem band + Bituach Leumi stipend
# ─────────────────────────────────────────────────────────────────────────


@router.get("/mekadem/{fund_id}")
def get_mekadem_band(
    fund_id: str,
    user_id: str,
    balance_nis: float | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """Mekadem variance band for the given Israeli pension fund.

    Returns ``{fund_id, typical, low, high}`` as serialized
    ValueWithRationale dicts. If ``balance_nis`` is supplied, also returns
    the corresponding ``annuity_monthly_nis_low/typical/high`` band so the
    UI can render the band directly on the cashflow chart's annuity line
    without re-computing.
    """
    try:
        band = get_mekadem_for_fund(
            fund_id,  # type: ignore[arg-type]
            user_id=user_id,
            session=db,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    out: dict = {
        "fund_id": band.fund_id,
        "typical": as_dict(band.typical),
        "low": as_dict(band.low),
        "high": as_dict(band.high),
    }
    if balance_nis is not None and balance_nis > 0:
        a_low, a_typ, a_high = monthly_annuity_for_band(
            band, balance_nis=balance_nis,
        )
        out["annuity_monthly_nis_low"] = as_dict(a_low)
        out["annuity_monthly_nis_typical"] = as_dict(a_typ)
        out["annuity_monthly_nis_high"] = as_dict(a_high)
    return out


# Wave 3 — projection trust layer
# ─────────────────────────────────────────────────────────────────────────


@router.get("/projection/ruin-probability")
def get_ruin_probability(
    user_id: str,
    retirement_age: float = 49.0,
    years: int = 40,
    target_p_solvent: float = 0.90,
    n_paths: int = 2000,
    seed: int | None = None,
    withdrawal_policy_id: str = "guyton_klinger",
    engine: str = "regime_switch",
    usd_fraction: float = 0.65,
    sigma_annual: float | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """Probability-of-ruin verdict with bootstrap CI.

    Returns the new "retirement readiness" hero verdict: P(solvent at 75/85/95)
    + 95% CI on the age-95 estimate + categorical verdict (ON_TRACK / OFF_TRACK
    / UNCERTAIN) + concrete suggested action.

    Replaces the prior single-month "retire-ready" verdict that ignored
    sequence-of-returns risk.
    """
    # When sigma_annual is None, compute_ruin_probability uses its default
    # (0.18 — diversified). When the UI passes the auto-calibrated value
    # from SigmaCalibrationCard, the engine uses that instead.
    kwargs: dict = dict(
        user_id=user_id,
        session=db,
        retirement_age=retirement_age,
        years=years,
        target_p_solvent=target_p_solvent,
        n_paths=n_paths,
        seed=seed,
        withdrawal_policy_id=withdrawal_policy_id,
        engine=engine,
        usd_fraction=usd_fraction,
    )
    if sigma_annual is not None:
        kwargs["sigma_annual"] = sigma_annual
    v = compute_ruin_probability(**kwargs)
    return {
        "p_solvent_at_75": as_dict(v.p_solvent_at_75),
        "p_solvent_at_85": as_dict(v.p_solvent_at_85),
        "p_solvent_at_95": as_dict(v.p_solvent_at_95),
        "p_solvent_at_95_ci_low": as_dict(v.p_solvent_at_95_ci_low),
        "p_solvent_at_95_ci_high": as_dict(v.p_solvent_at_95_ci_high),
        "target_p_solvent": as_dict(v.target_p_solvent),
        "verdict": v.verdict,
        "suggested_action": as_dict(v.suggested_action),
    }


# Wave 4 — decision policy
# ─────────────────────────────────────────────────────────────────────────


@router.get("/glide-path")
def get_glide_path(
    policy: str = "vanguard_target_date",
    start_age: int = 30,
    end_age: int = 95,
) -> dict:
    """Per-age target equity/bond/cash allocation table."""
    path = compute_glide_path(
        start_age=start_age, end_age=end_age, policy=policy,  # type: ignore[arg-type]
    )
    return {
        "policy": policy,
        "points": [
            {
                "age": p.age,
                "target_equity_pct": as_dict(p.target_equity_pct),
                "target_bond_pct": as_dict(p.target_bond_pct),
                "target_cash_pct": as_dict(p.target_cash_pct),
            }
            for p in path
        ],
    }


@router.get("/rebalancing-alerts")
def get_rebalancing_alerts(
    user_id: str,
    current_age: int,
    db: Session = Depends(get_db),
) -> dict:
    """Per-class drift alerts vs glide-path target."""
    alerts = detect_rebalancing_alerts(
        user_id=user_id, current_age=current_age, session=db,
    )
    return {
        "alerts": [
            {
                "asset_class": a.asset_class,
                "current_pct": as_dict(a.current_pct),
                "target_pct": as_dict(a.target_pct),
                "drift_pp": as_dict(a.drift_pp),
                "rule_fired": a.rule_fired,
                "suggested_proposal": a.suggested_proposal,
            }
            for a in alerts
        ],
    }


@router.get("/phase-expenses")
def get_phase_expenses(
    has_kids: bool = True,
    kids_birth_year_1: int | None = None,
    kids_birth_year_2: int | None = None,
) -> dict:
    """Phase-based expense curve + (optionally) IDF service phases."""
    phases = build_phase_expense_curve(has_kids=has_kids)
    kids_years = [
        y for y in (kids_birth_year_1, kids_birth_year_2) if y is not None
    ]
    idf = idf_service_phases(kids_birth_years=kids_years)
    return {
        "phases": [
            {
                "start_age": p.start_age,
                "end_age": p.end_age,
                "label": p.label,
                "monthly_multiplier": as_dict(p.monthly_multiplier),
                "inflation_premium": as_dict(p.inflation_premium),
            }
            for p in phases + idf
        ],
    }


@router.get("/lifecycle-income")
def get_lifecycle_income(
    current_age: float,
    partner_income_monthly_nis: float = 0.0,
    side_income_monthly_nis: float = 0.0,
    unemployment_annual_probability: float = 0.05,
) -> dict:
    """Lifecycle income event timeline."""
    events = build_lifecycle_timeline(
        current_age=current_age,
        partner_income_monthly_nis=partner_income_monthly_nis,
        side_income_monthly_nis=side_income_monthly_nis,
        unemployment_annual_probability=unemployment_annual_probability,
    )
    return {
        "events": [
            {
                "age": e.age,
                "event_type": e.event_type,
                "monthly_impact_nis": as_dict(e.monthly_impact_nis),
                "probability": as_dict(e.probability),
                "rationale": e.rationale,
            }
            for e in events
        ],
    }


@router.get("/healthcare-curve")
def get_healthcare_curve(
    start_age: int = 30,
    end_age: int = 95,
    monthly_burn_nis: float = 0.0,
) -> dict:
    """Age-banded healthcare cost curve + optional share-of-burn calc."""
    curve = build_healthcare_curve(start_age=start_age, end_age=end_age)
    share_at_70 = (
        healthcare_share_of_burn(age=70, monthly_burn_nis=monthly_burn_nis)
        if monthly_burn_nis > 0
        else None
    )
    return {
        "curve": [
            {"age": p.age, "monthly_cost_nis": as_dict(p.monthly_cost_nis)}
            for p in curve
        ],
        "share_of_burn_at_70": as_dict(share_at_70) if share_at_70 else None,
    }


# Wave 5 — tax engine + hishtalmut + decumulation + lump-vs-annuity
# ─────────────────────────────────────────────────────────────────────────


@router.post("/tax/compute")
def post_tax_compute(
    payload: dict,
    user_id: str,
    year: int = 2026,
    db: Session = Depends(get_db),
) -> dict:
    """Compute tax breakdown for a single cashflow.

    payload schema:
      {source, gross_amount_nis, account?, holding_years?, user_age?,
       us_gross_amount_for_treaty?, is_post_67?}
    """
    try:
        cashflow = TaxableCashflow(
            source=payload["source"],
            gross_amount_nis=float(payload["gross_amount_nis"]),
            account=payload.get("account", "taxable"),
            holding_years=int(payload.get("holding_years", 0)),
            user_age=int(payload.get("user_age", 40)),
            us_gross_amount_for_treaty=float(
                payload.get("us_gross_amount_for_treaty", 0.0),
            ),
            is_post_67=bool(payload.get("is_post_67", False)),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    tb = compute_tax(cashflow, user_id=user_id, session=db, year=year)
    return {
        "gross": as_dict(tb.gross),
        "net": as_dict(tb.net),
        "israeli_tax": as_dict(tb.israeli_tax),
        "us_treaty_credit": as_dict(tb.us_treaty_credit),
        "bituach_leumi_tax": as_dict(tb.bituach_leumi_tax),
        "effective_rate": as_dict(tb.effective_rate),
    }


@router.get("/hishtalmut/eligibility")
def get_hishtalmut_eligibility(
    user_id: str,
    first_deposit_date_iso: str,
    user_current_age: int,
    db: Session = Depends(get_db),
) -> dict:
    """Hishtalmut tax-free eligibility (6yr OR age-67 paths)."""
    el = check_hishtalmut_eligibility(
        user_id=user_id, session=db,
        first_deposit_date_iso=first_deposit_date_iso,
        user_current_age=user_current_age,
    )
    return {
        "months_until_taxfree": as_dict(el.months_until_taxfree),
        "first_deposit_date": as_dict(el.first_deposit_date),
        "six_yr_eligible": as_dict(el.six_yr_eligible),
        "age_67_eligible": as_dict(el.age_67_eligible),
        "taxfree_now": as_dict(el.taxfree_now),
        "early_withdrawal_marginal_rate": as_dict(el.early_withdrawal_marginal_rate),
    }


@router.get("/hishtalmut/withdrawal-tax")
def get_hishtalmut_withdrawal_tax(
    user_id: str,
    first_deposit_date_iso: str,
    user_current_age: int,
    gross_nis: float,
    db: Session = Depends(get_db),
) -> dict:
    """Tax owed on a hypothetical hishtalmut withdrawal."""
    el = check_hishtalmut_eligibility(
        user_id=user_id, session=db,
        first_deposit_date_iso=first_deposit_date_iso,
        user_current_age=user_current_age,
    )
    tax = tax_on_hishtalmut_withdrawal(gross_nis=gross_nis, eligibility=el)
    return {"tax": as_dict(tax), "taxfree_now": int(el.taxfree_now.value or 0)}


@router.get("/decumulation/order")
def get_decumulation_order(
    monthly_need_nis: float,
    taxable_balance_nis: float = 0.0,
    hishtalmut_balance_nis: float = 0.0,
    kupat_gemel_balance_nis: float = 0.0,
    pensia_annuity_monthly_nis: float = 0.0,
) -> dict:
    steps = optimize_decumulation_order(
        monthly_need_nis=monthly_need_nis,
        taxable_balance_nis=taxable_balance_nis,
        hishtalmut_balance_nis=hishtalmut_balance_nis,
        kupat_gemel_balance_nis=kupat_gemel_balance_nis,
        pensia_annuity_monthly_nis=pensia_annuity_monthly_nis,
    )
    return {
        "steps": [
            {
                "order": s.order,
                "account": s.account,
                "monthly_draw_nis": as_dict(s.monthly_draw_nis),
                "rationale": s.rationale,
            }
            for s in steps
        ],
    }


@router.get("/lump-vs-annuity")
def get_lump_vs_annuity(
    pension_balance_nis: float,
    mekadem_typical: float = 200.0,
    monthly_expense_need_nis: float = 20_000.0,
    years_remaining: int = 28,
    real_return_annual: float = 0.03,
) -> dict:
    v = compute_lump_vs_annuity(
        pension_balance_nis=pension_balance_nis,
        mekadem_typical=mekadem_typical,
        monthly_expense_need_nis=monthly_expense_need_nis,
        years_remaining=years_remaining,
        real_return_annual=real_return_annual,
    )
    return {
        "recommendation": v.recommendation,
        "annuity_path": v.annuity_path,
        "lump_path": v.lump_path,
        "split_path": v.split_path,
        "rationale": as_dict(v.rationale),
    }


# Wave 6 — balance sheet
# ─────────────────────────────────────────────────────────────────────────


@router.get("/real-estate")
def get_real_estate(
    primary_residence_value_nis: float = 0.0,
    mortgage_balance_nis: float = 0.0,
    monthly_property_tax_nis: float = 0.0,
    appreciation_annual: float = 0.035,
) -> dict:
    rs = extract_real_estate_state(
        primary_residence_value_nis=primary_residence_value_nis,
        mortgage_balance_nis=mortgage_balance_nis,
        monthly_property_tax_nis=monthly_property_tax_nis,
        appreciation_annual=appreciation_annual,
    )
    return {
        "primary_residence_value_nis": as_dict(rs.primary_residence_value_nis),
        "mortgage_balance_nis": as_dict(rs.mortgage_balance_nis),
        "equity_nis": as_dict(rs.equity_nis),
        "appreciation_annual": as_dict(rs.appreciation_annual),
        "illiquidity_haircut": as_dict(rs.illiquidity_haircut),
        "monthly_property_tax_nis": as_dict(rs.monthly_property_tax_nis),
    }


@router.get("/mortgage/schedule")
def get_mortgage_schedule(
    initial_balance_nis: float,
    annual_rate: float,
    term_months: int,
    sample_every_months: int = 12,
) -> dict:
    """Return the amortization schedule (sampled to keep payload small)."""
    full = build_mortgage_schedule(
        initial_balance_nis=initial_balance_nis,
        annual_rate=annual_rate,
        term_months=term_months,
    )
    sampled = [
        {
            "month": r.month,
            "payment_nis": r.payment_nis.value,
            "principal_paid_nis": r.principal_paid_nis.value,
            "interest_paid_nis": r.interest_paid_nis.value,
            "remaining_balance_nis": r.remaining_balance_nis.value,
        }
        for i, r in enumerate(full)
        if i % sample_every_months == 0 or i == len(full) - 1
    ]
    total_interest = sum(r.interest_paid_nis.value or 0 for r in full)
    return {
        "rows": sampled,
        "term_months": term_months,
        "total_interest_nis": round(total_interest, 2),
    }


@router.get("/partner")
def get_partner(
    age_years: float = 0.0,
    monthly_income_nis: float = 0.0,
    pension_balance_nis: float = 0.0,
    retirement_age: float = 67.0,
    is_eligible_for_bl_supplement: bool = False,
    primary_retire_age: float = 49.0,
) -> dict:
    p = extract_partner_state(
        age_years=age_years,
        monthly_income_nis=monthly_income_nis,
        pension_balance_nis=pension_balance_nis,
        retirement_age=retirement_age,
        is_eligible_for_bl_supplement=is_eligible_for_bl_supplement,
    )
    household = household_retire_ready_age(
        primary_retire_age=primary_retire_age, partner=p,
    )
    if p is None:
        return {"partner": None, "household_retire_ready_age": as_dict(household)}
    return {
        "partner": {
            "age_years": as_dict(p.age_years),
            "monthly_income_nis": as_dict(p.monthly_income_nis),
            "pension_balance_nis": as_dict(p.pension_balance_nis),
            "retirement_age": as_dict(p.retirement_age),
            "is_eligible_for_bl_supplement": as_dict(p.is_eligible_for_bl_supplement),
        },
        "household_retire_ready_age": as_dict(household),
    }


@router.get("/severance")
def get_severance(
    accrued_pizurim_nis: float = 0.0,
    withdrawn_history_nis: float = 0.0,
    annuitization_probability: float = 0.50,
    kupat_pensia_balance_nis: float = 0.0,
) -> dict:
    sev = extract_severance_state(
        accrued_pizurim_nis=accrued_pizurim_nis,
        withdrawn_history_nis=withdrawn_history_nis,
        annuitization_probability=annuitization_probability,
    )
    effective = effective_pension_for_annuity(
        kupat_pensia_balance_nis=kupat_pensia_balance_nis,
        severance=sev,
    )
    return {
        "accrued_pizurim_nis": as_dict(sev.accrued_pizurim_nis),
        "withdrawn_history_nis": as_dict(sev.withdrawn_history_nis),
        "annuitization_probability": as_dict(sev.annuitization_probability),
        "tax_treatment": as_dict(sev.tax_treatment),
        "effective_pension_for_annuity_nis": as_dict(effective),
    }


# Wave 7 — companion UX
# ─────────────────────────────────────────────────────────────────────────


@router.get("/insurance-gaps")
def get_insurance_gaps(
    monthly_income_nis: float,
    monthly_expenses_nis: float,
    dependents_count: int,
    has_kids_under_18: bool,
    assets_nis: float,
    actual_life_coverage_nis: float = 0.0,
    actual_disability_monthly_nis: float = 0.0,
    actual_ltc_monthly_nis: float = 0.0,
    actual_health_supplementary: bool = False,
) -> dict:
    gaps = compute_insurance_gaps(
        monthly_income_nis=monthly_income_nis,
        monthly_expenses_nis=monthly_expenses_nis,
        dependents_count=dependents_count,
        has_kids_under_18=has_kids_under_18,
        assets_nis=assets_nis,
        actual_life_coverage_nis=actual_life_coverage_nis,
        actual_disability_monthly_nis=actual_disability_monthly_nis,
        actual_ltc_monthly_nis=actual_ltc_monthly_nis,
        actual_health_supplementary=actual_health_supplementary,
    )
    return {
        "gaps": [
            {
                "insurance_type": g.insurance_type,
                "recommended_coverage": as_dict(g.recommended_coverage_nis),
                "actual_coverage": as_dict(g.actual_coverage_nis),
                "gap": as_dict(g.gap_nis),
                "suggested_action": as_dict(g.suggested_action),
            }
            for g in gaps
        ],
    }


@router.get("/replan-triggers")
def get_replan_triggers_registry() -> dict:
    """Known trigger kinds + descriptions."""
    return {"triggers": list_known_triggers()}


@router.post("/multi-goal/balance")
def post_multi_goal_balance(payload: dict) -> dict:
    """Balance multi-goal allocation with hard/soft constraints.

    payload: {available_capital_nis, constraints: [{goal_id, constraint_type,
              target_nis, deadline, priority, rationale}, ...]}
    """
    try:
        constraints = [
            GoalConstraint(
                goal_id=c["goal_id"],
                constraint_type=c["constraint_type"],
                target_nis=float(c["target_nis"]),
                deadline=c.get("deadline"),
                priority=int(c.get("priority", 5)),
                rationale=c.get("rationale", ""),
            )
            for c in payload.get("constraints", [])
        ]
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = balance_multi_goals(
        available_capital_nis=float(payload["available_capital_nis"]),
        constraints=constraints,
    )
    return {
        "balances": [
            {
                "goal_id": b.goal_id,
                "target_nis": b.target_nis,
                "funded_pct": b.funded_pct,
                "binding_constraints": b.binding_constraints,
                "tradeoffs": b.tradeoffs,
            }
            for b in result
        ],
    }


@router.get("/behavioral/check")
def get_behavioral_check(
    kind: str,
    proposed_pct: float,
    secondary_pct: float = 0.0,
    days_since: int = 0,
) -> dict:
    """Run a behavioral checkpoint:

    kind='panic_sell': proposed_pct=sell_pct, secondary_pct=drawdown_pct, days_since=days_since_peak
    kind='fomo_buy':   proposed_pct=buy_pct, secondary_pct=asset_30d_return_pct,
                       (concentration via separate query param)
    """
    if kind == "panic_sell":
        cp = check_panic_sell(
            proposed_sell_pct=proposed_pct,
            days_since_market_peak=days_since,
            peak_to_now_drawdown_pct=secondary_pct,
        )
    elif kind == "fomo_buy":
        cp = check_fomo_buy(
            proposed_buy_pct=proposed_pct,
            asset_30d_return_pct=secondary_pct,
            asset_concentration_pct=days_since / 100.0 if days_since > 1 else 0.0,
        )
    else:
        raise HTTPException(status_code=400, detail=f"unknown kind: {kind!r}")
    return {
        "kind": cp.kind,
        "triggered": cp.triggered,
        "rationale": cp.rationale,
        "cooldown_hours": cp.cooldown_hours,
        "confirmation_prompt": cp.confirmation_prompt,
    }


@router.get("/projection/sigma-calibrated")
def get_sigma_calibration(
    user_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Portfolio-weighted sigma + per-class breakdown.

    Replaces the hardcoded σ=0.18 default for users with non-diversified
    portfolios. For Ariel's NVDA-heavy holdings, returns σ closer to 0.30-0.40.
    """
    cal = calibrate_sigma_from_holdings(user_id=user_id, session=db)
    return {
        "sigma_annual": as_dict(cal.sigma_annual),
        "portfolio_total_usd": as_dict(cal.portfolio_total_usd),
        "breakdown": cal.breakdown,
    }


@router.get("/projection/withdrawal-policies")
def get_withdrawal_policies() -> dict:
    """List shipped withdrawal policies (Bengen/Guyton-Klinger/VPW/Bucket)."""
    return {"policies": list_policies()}


@router.get("/projection/stochastic-fx")
def get_stochastic_fx(
    initial_fx: float,
    months: int = 360,
    n_paths: int = 1000,
    seed: int | None = 42,
) -> dict:
    """Stochastic USD/NIS forecast — percentile bands at the horizon."""
    sim = simulate_stochastic_fx(
        initial_fx=initial_fx, months=months, n_paths=n_paths, seed=seed,
    )
    bands = fx_band_at_horizon(sim)
    return {
        "horizon_months": months,
        "initial_fx": initial_fx,
        "bands": {k: as_dict(v) for k, v in bands.items()},
    }


# Windfall flow — closes user-guide hole #1 (Add income event discoverability)
# ─────────────────────────────────────────────────────────────────────────


def _latest_two_tsvs(tsv_root: Path) -> tuple[Path | None, Path | None]:
    """Return (latest_tsv, previous_tsv) by file-modified time. Filters by
    the canonical 'Family Finances Status - * .tsv' prefix."""
    if not tsv_root.exists():
        return None, None
    tsvs = sorted(
        tsv_root.glob("Family Finances Status - *.tsv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return (
        tsvs[0] if len(tsvs) >= 1 else None,
        tsvs[1] if len(tsvs) >= 2 else None,
    )


@router.get("/windfall/detect")
def get_windfall_detect(
    threshold_usd: float = DEFAULT_THRESHOLD_USD,
    threshold_nis: float = DEFAULT_THRESHOLD_NIS,
) -> dict:
    """Detect a windfall by diffing the two most recent TSV snapshots.

    Returns the detected event + a ranked allocation plan (long/medium/
    short horizons). When no event is found, returns ``{"event": None}``.

    User spec (2026-05-28):
      - Fires only when cash delta > $25K USD OR > ₪75K NIS
      - Auto-classifies via matching equity sales (5% tolerance)
      - Long-term proposals close plan-target gaps (deterministic)
      - Medium + short horizons handed off to the agent fleet (placeholder)
    """
    import os
    # 2026-05-29: the hard-coded Google Drive fallback was a single-user
    # workaround that broke any other tenant. Default now resolves to a
    # project-internal directory under ARGOSY_HOME -- this is where the
    # /portfolio upload tile (POST /portfolio/upload-snapshot) writes
    # snapshots. The env var remains an opt-in override for users who
    # still maintain their TSVs in an external folder.
    env_root = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
    if env_root:
        samples_root = Path(env_root)
    else:
        from argosy.config import get_settings
        samples_root = get_settings().home / "snapshots"
    cur, prev = _latest_two_tsvs(samples_root)
    if cur is None or prev is None:
        return {"event": None, "reason": "fewer than 2 TSVs found"}

    event = detect_windfall(
        cur, prev,
        threshold_usd=threshold_usd,
        threshold_nis=threshold_nis,
    )
    if event is None:
        return {
            "event": None,
            "reason": "no windfall above threshold",
            "current_tsv": cur.name,
            "previous_tsv": prev.name,
        }

    plan = propose_allocations(event)
    return {
        "event": {
            "detected_at": event.detected_at.isoformat(),
            "cash_delta_usd": event.cash_delta_usd,
            "cash_delta_nis": event.cash_delta_nis,
            "cash_delta_total_usd_equiv": event.cash_delta_total_usd_equiv,
            "fx_usd_nis": event.fx_usd_nis,
            "classified_source": event.classified_source,
            "requires_user_classification": event.requires_user_classification,
            "matching_sales": [
                {
                    "symbol": s.symbol,
                    "shares_sold": s.shares_sold,
                    "current_price": s.current_price,
                    "value_usd": round(s.value_usd, 2),
                }
                for s in event.matching_sales
            ],
            "allocation_delta_table": [
                {
                    "asset_class": l.asset_class,
                    "current_pct": l.current_pct,
                    "current_k_usd": l.current_k_usd,
                    "target_pct": l.target_pct,
                    "target_k_usd": l.target_k_usd,
                    "delta_k_usd": l.delta_k_usd,
                }
                for l in event.allocation_delta_table
            ],
            "source_tsv": Path(event.source_tsv).name,
            "previous_tsv": Path(event.previous_tsv).name if event.previous_tsv else None,
        },
        "plan": plan.to_dict(),
    }


# Windfall Accept / Defer (closes user-guide Hole #2, 2026-05-29)
# ─────────────────────────────────────────────────────────────────────────


class WindfallActionRequest(BaseModel):
    """Payload for POST /windfall/accept and POST /windfall/defer.

    Wire-format backward-compatibility shim: the UI (as of 2026-05-29)
    still POSTs `event_detected_at` + `event_source_tsv` from the
    WindfallEventDTO. Internally these map to `source_detected_at` +
    `source_ref` on the renamed `allocation_actions` table with
    `action_source='windfall'`. The UI rename + new
    /api/proposals/allocation/{accept,defer} routes land in sprint
    commit #6 alongside the WindfallCard mount move.
    """

    user_id: str
    event_detected_at: datetime
    event_source_tsv: str
    horizon: str
    asset_class: str
    instrument: str
    amount_usd: float
    rationale: str
    closes_delta_usd: float
    confidence: str
    # /defer-only: when does the user want to be re-prompted?
    due_date: date | None = None
    user_note: str | None = None


class WindfallActionResponse(BaseModel):
    id: int
    decided_status: str
    decided_at: datetime
    due_date: date | None


def _create_windfall_action(
    payload: WindfallActionRequest,
    decided_status: str,
    db: Session,
) -> "AllocationAction":
    """Shared helper for /accept and /defer.

    Constructs an `AllocationAction` row with `action_source='windfall'`,
    mapping legacy payload fields to the renamed columns:

        payload.event_detected_at → row.source_detected_at
        payload.event_source_tsv  → row.source_ref
    """
    from argosy.state.models import AllocationAction

    row = AllocationAction(
        user_id=payload.user_id,
        action_source="windfall",
        source_detected_at=payload.event_detected_at,
        source_ref=payload.event_source_tsv,
        horizon=payload.horizon,
        asset_class=payload.asset_class,
        instrument=payload.instrument,
        amount_usd=payload.amount_usd,
        rationale=payload.rationale,
        closes_delta_usd=payload.closes_delta_usd,
        confidence=payload.confidence,
        decided_status=decided_status,
        due_date=payload.due_date,
        user_note=payload.user_note,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.post("/windfall/accept", response_model=WindfallActionResponse)
def post_windfall_accept(
    payload: WindfallActionRequest,
    db: Session = Depends(get_db),
) -> WindfallActionResponse:
    """Record an Accept on a windfall allocation proposal.

    Persistence-only for now: a row lands in `allocation_actions` with
    `decided_status='accepted'` and `action_source='windfall'`. Promotion
    into an action_engine PrioritizedAction ships in a follow-up commit.
    """
    row = _create_windfall_action(payload, "accepted", db)
    return WindfallActionResponse(
        id=row.id,
        decided_status=row.decided_status,
        decided_at=row.decided_at,
        due_date=row.due_date,
    )


@router.post("/windfall/defer", response_model=WindfallActionResponse)
def post_windfall_defer(
    payload: WindfallActionRequest,
    db: Session = Depends(get_db),
) -> WindfallActionResponse:
    """Record a Defer on a windfall allocation proposal.

    Same row shape as /accept; `decided_status='deferred'`. Optional
    `due_date` stamps when the user wants to be re-prompted. Without a
    `due_date`, the row sits as an open deferral until either Accept
    or expiration.
    """
    row = _create_windfall_action(payload, "deferred", db)
    return WindfallActionResponse(
        id=row.id,
        decided_status=row.decided_status,
        decided_at=row.decided_at,
        due_date=row.due_date,
    )


class WindfallActionListItem(BaseModel):
    id: int
    event_detected_at: datetime
    event_source_tsv: str
    horizon: str
    asset_class: str
    instrument: str
    amount_usd: float
    decided_status: str
    decided_at: datetime
    due_date: date | None
    user_note: str | None
    proposal_id: int | None


class WindfallActionsListResponse(BaseModel):
    actions: list[WindfallActionListItem]


@router.get("/windfall/actions", response_model=WindfallActionsListResponse)
def list_windfall_actions(
    user_id: str,
    event_detected_at: datetime | None = None,
    event_source_tsv: str | None = None,
    db: Session = Depends(get_db),
) -> WindfallActionsListResponse:
    """List recorded windfall actions for a user.

    Optional filters by event (`event_detected_at` + `event_source_tsv`)
    let the UI fetch only the actions belonging to the currently-
    displayed event. Filters map to the renamed columns
    (`source_detected_at` / `source_ref`) and additionally constrain
    `action_source='windfall'` so other-source rows don't surface here.
    """
    from argosy.state.models import AllocationAction

    q = (
        db.query(AllocationAction)
        .filter(AllocationAction.user_id == user_id)
        .filter(AllocationAction.action_source == "windfall")
    )
    if event_detected_at is not None:
        q = q.filter(AllocationAction.source_detected_at == event_detected_at)
    if event_source_tsv is not None:
        q = q.filter(AllocationAction.source_ref == event_source_tsv)
    rows = q.order_by(AllocationAction.decided_at.desc()).all()
    return WindfallActionsListResponse(
        actions=[
            WindfallActionListItem(
                id=r.id,
                event_detected_at=r.source_detected_at,
                event_source_tsv=r.source_ref or "",
                horizon=r.horizon,
                asset_class=r.asset_class,
                instrument=r.instrument,
                amount_usd=float(r.amount_usd),
                decided_status=r.decided_status,
                decided_at=r.decided_at,
                due_date=r.due_date,
                user_note=r.user_note,
                proposal_id=r.proposal_id,
            )
            for r in rows
        ],
    )


# Wave 2 — safety gates
# ─────────────────────────────────────────────────────────────────────────


@router.get("/safety-gates")
def get_safety_gates(
    user_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Returns the list of safety-gate verdicts (NRA estate + Liquidity in Wave 2).

    Each verdict is a dict with: gate_id, status (PASS/WARN/FAIL),
    value, threshold, suggested_action, detail_summary.
    """
    verdicts = compute_safety_gates(user_id=user_id, session=db)
    return {
        "gates": [
            {
                "gate_id": v.gate_id,
                "status": v.status,
                "value": as_dict(v.value),
                "threshold": as_dict(v.threshold),
                "suggested_action": as_dict(v.suggested_action),
                "detail_summary": v.detail_summary,
            }
            for v in verdicts
        ],
    }


@router.get("/bituach-leumi")
def get_bituach_leumi(
    user_id: str,
    current_age: int,
    contribution_history_years: int,
    spouse_eligible: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    """BL old-age stipend estimate with low/typical/high bands + sensitivity levers."""
    est = estimate_bl_stipend(
        current_age=current_age,
        contribution_history_years=contribution_history_years,
        spouse_eligible=spouse_eligible,
        user_id=user_id,
        session=db,
    )
    return {
        "monthly_nis": as_dict(est.monthly_nis),
        "monthly_nis_low": as_dict(est.monthly_nis_low),
        "monthly_nis_high": as_dict(est.monthly_nis_high),
        "eligibility_age": as_dict(est.eligibility_age),
        "contribution_history_factor": as_dict(est.contribution_history_factor),
        "spouse_supplement_applied": as_dict(est.spouse_supplement_applied),
        "sensitivity_levers": est.sensitivity_levers,
    }


# Sprint commit #10 — Holistic Timeline data source
# ─────────────────────────────────────────────────────────────────────────


class VestMarkerDTO(BaseModel):
    """ISO-date wire format mirroring services.retirement_timeline.VestMarker."""

    kind: str
    date: str
    symbol: str
    grant_id: str
    shares: float
    fmv_per_share_usd: float | None = None
    estimated_gross_usd: float | None = None


class LifeEventMarkerDTO(BaseModel):
    """ISO-date wire format mirroring services.retirement_timeline.LifeEventMarker."""

    date: str
    category: str
    kind: str
    amount_usd: float | None = None
    description: str | None = None


class RetireZoneDTO(BaseModel):
    """ISO-date wire format mirroring services.retirement_timeline.RetireZone."""

    scenario: str
    age_years: float
    expected_date: str
    clamp_reason: str


class HolisticTimelineDTO(BaseModel):
    """Composite payload for the <HolisticTimelineCard> consumer."""

    today: str
    past_vests: list[VestMarkerDTO]
    future_vests: list[VestMarkerDTO]
    life_events: list[LifeEventMarkerDTO]
    retire_ready_zones: list[RetireZoneDTO]


@router.get("/timeline", response_model=HolisticTimelineDTO)
def get_holistic_timeline(
    user_id: str,
    horizon_days: int = 365 * 30,
    db: Session = Depends(get_db),
) -> HolisticTimelineDTO:
    """Composite timeline payload for the <HolisticTimelineCard>.

    Returns 200 with empty arrays when the user has no vests / life
    events / projection crossing -- the UI shows an onboarding nudge
    rather than an error in that case.
    """
    payload = build_holistic_timeline(
        session=db,
        user_id=user_id,
        horizon_days=horizon_days,
    )
    return HolisticTimelineDTO(
        today=payload.today.isoformat(),
        past_vests=[
            VestMarkerDTO(
                kind=v.kind,
                date=v.date.isoformat(),
                symbol=v.symbol,
                grant_id=v.grant_id,
                shares=v.shares,
                fmv_per_share_usd=v.fmv_per_share_usd,
                estimated_gross_usd=v.estimated_gross_usd,
            )
            for v in payload.past_vests
        ],
        future_vests=[
            VestMarkerDTO(
                kind=v.kind,
                date=v.date.isoformat(),
                symbol=v.symbol,
                grant_id=v.grant_id,
                shares=v.shares,
                fmv_per_share_usd=v.fmv_per_share_usd,
                estimated_gross_usd=v.estimated_gross_usd,
            )
            for v in payload.future_vests
        ],
        life_events=[
            LifeEventMarkerDTO(
                date=e.date.isoformat(),
                category=e.category,
                kind=e.kind,
                amount_usd=e.amount_usd,
                description=e.description,
            )
            for e in payload.life_events
        ],
        retire_ready_zones=[
            RetireZoneDTO(
                scenario=z.scenario,
                age_years=z.age_years,
                expected_date=z.expected_date.isoformat(),
                clamp_reason=z.clamp_reason,
            )
            for z in payload.retire_ready_zones
        ],
    )


# ---------------------------------------------------------------------------
# Monitor agent endpoints (sprint commit #11) -- allocation-drift trigger.
# ---------------------------------------------------------------------------


@router.post("/monitor/run")
def post_run_drift_check(user_id: str, db: Session = Depends(get_db)) -> dict:
    """Manually trigger an allocation-drift check (spec §5.1.1).

    In production this runs nightly via cron; the route lets the UI
    surface a "check now" button and lets tests drive the detector
    deterministically. Returns the ``DriftCheckResult`` payload (flags
    fired this run + the snapshot that was scored).
    """
    result = check_allocation_drift(db, user_id)
    return result.to_dict()


@router.get("/monitor/flags")
def list_monitor_flags(user_id: str, db: Session = Depends(get_db)) -> list[dict]:
    """List active monitor flags for ``user_id`` (Home Red-Flag Strip).

    Active = unacknowledged AND unexpired. Sprint commit #17 will consume
    this from the UI. Returns both allocation-drift and MC-regression
    flags interleaved; the UI sorts by severity + surfaced_at.
    """
    drift = [f.to_dict() for f in get_active_drift_flags(db, user_id)]
    mc = [f.to_dict() for f in get_active_mc_regression_flags(db, user_id)]
    # Tag the kind so the UI can render the right card.
    for d in drift:
        d["kind"] = "allocation_drift"
    for m in mc:
        m["kind"] = "mc_regression"
    return drift + mc


# Sprint commit #12 — Monte-Carlo regression trigger (spec §5.1.2).
# ─────────────────────────────────────────────────────────────────────────


@router.post("/monitor/mc-check")
def post_run_mc_check(
    user_id: str,
    force: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    """Trigger the monthly MC regression check (spec §5.1.2).

    Production runs this via cron on the 1st of each month. The first
    call for a user persists a baseline anchor and returns
    ``flag_fired=None``; subsequent calls compare against the prior
    persisted ``curr_p_solvent`` and fire a flag when P(solvent at 95)
    has dropped by >= 5 percentage points.

    Cooldown guard (codex IMPORTANT, commit #11+#12 review): MC
    projection is expensive (numpy paths × n_paths). The endpoint
    enforces a per-user monthly cooldown by default — if a prior
    mc_regression row was written within the last 25 days, return its
    cached payload instead of re-running. Pass ``force=true`` to
    override (cron-driven runs do this; UI manual triggers don't).
    """
    from argosy.services.plan_monitor import _latest_mc_regression_row
    from datetime import datetime, timezone, timedelta

    if not force:
        latest = _latest_mc_regression_row(db, user_id)
        if latest is not None:
            now = datetime.now(timezone.utc)
            latest_at = latest.surfaced_at
            if latest_at.tzinfo is None:
                latest_at = latest_at.replace(tzinfo=timezone.utc)
            if now - latest_at < timedelta(days=25):
                # Recent run cached — return its payload.
                import json
                payload = json.loads(latest.payload)
                return {
                    "flag_fired": None if not payload.get("fired") else {
                        "user_id": user_id,
                        "snapshot_date": payload.get("snapshot_date"),
                        "prev_p_solvent": payload.get("prev_p_solvent"),
                        "curr_p_solvent": payload.get("curr_p_solvent"),
                        "delta_pp": payload.get("delta_pp"),
                        "severity": latest.severity,
                    },
                    "prev_run_date": payload.get("snapshot_date"),
                    "curr_p_solvent": payload.get("curr_p_solvent"),
                    "rows_evaluated": 1,
                    "cached": True,
                    "cached_age_days": (now - latest_at).days,
                }

    result = check_mc_regression(db, user_id).to_dict()
    result["cached"] = False
    return result
