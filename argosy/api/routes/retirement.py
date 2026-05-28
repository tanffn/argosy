"""Umbrella router for retirement-engine endpoints (Wave 0+).

Wave 0 surfaces only the sources + reference primitives. Later waves
register additional endpoints on this same ``/api/retirement/*`` prefix
without touching the cross-cutting plumbing here.

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``.
"""
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
from argosy.services.retirement.withdrawal_policy import list_policies

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
    db: Session = Depends(get_db),
) -> dict:
    """Probability-of-ruin verdict with bootstrap CI.

    Returns the new "retirement readiness" hero verdict: P(solvent at 75/85/95)
    + 95% CI on the age-95 estimate + categorical verdict (ON_TRACK / OFF_TRACK
    / UNCERTAIN) + concrete suggested action.

    Replaces the prior single-month "retire-ready" verdict that ignored
    sequence-of-returns risk.
    """
    v = compute_ruin_probability(
        user_id=user_id,
        session=db,
        retirement_age=retirement_age,
        years=years,
        target_p_solvent=target_p_solvent,
        n_paths=n_paths,
        seed=seed,
    )
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
