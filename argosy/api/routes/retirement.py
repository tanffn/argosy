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
from argosy.services.retirement.sources import load_sources

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
