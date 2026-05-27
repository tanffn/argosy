"""GET /api/portfolio/wealth-dashboard — top-of-/portfolio aggregator.

Wraps the pure-Python ``compute_wealth_dashboard`` service in a sync
FastAPI route. No agent calls, no LLM — all data is sourced from the DB.

Response shape mirrors the dataclasses in
``argosy.services.wealth_dashboard``; we serialise via ``asdict`` and
let pydantic re-validate the payload.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.wealth_dashboard import (
    WealthDashboard,
    compute_wealth_dashboard,
    wealth_dashboard_to_dict,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio", "wealth-dashboard"])


# ---------------------------------------------------------------------------
# DTOs — re-declared as pydantic models so FastAPI generates the schema
# correctly and the route response is validated. Kept structurally
# identical to the service-layer dataclasses (asdict round-trips).
# ---------------------------------------------------------------------------


class ScenarioCardDTO(BaseModel):
    name: str
    real_return: float
    years_to_target: float | None
    target_age: int | None
    target_portfolio_nis: float | None


class TrajectoryPointDTO(BaseModel):
    year: int
    bear: float
    conservative: float
    typical: float


class RetirementBlockDTO(BaseModel):
    net_worth_nis: float | None
    net_worth_usd: float | None
    monthly_burn_nis: float | None
    monthly_income_nis: float | None
    monthly_surplus_nis: float | None
    annual_expenses_nis: float | None
    target_portfolio_nis: float | None
    swr_rate: float
    current_age: int
    current_age_inferred: bool
    scenarios: list[ScenarioCardDTO]
    trajectory: list[TrajectoryPointDTO]
    missing_reasons: list[str]


class CashRunwayBlockDTO(BaseModel):
    cash_nis: float | None
    sgov_nis: float | None
    defensive_total_nis: float | None
    months_of_runway: float | None
    missing_reasons: list[str]


class ConcentrationBlockDTO(BaseModel):
    symbol: str
    current_pct: float | None
    target_pct: float | None
    target_source: str | None
    missing_reasons: list[str]


class SavingsRateBlockDTO(BaseModel):
    monthly_income_nis: float | None
    monthly_burn_nis: float | None
    rate_pct: float | None
    missing_reasons: list[str]


class FxBucketDTO(BaseModel):
    currency: str
    value_nis: float
    pct: float


class FxExposureBlockDTO(BaseModel):
    buckets: list[FxBucketDTO]
    usd_pct: float | None
    missing_reasons: list[str]


class RsuQuarterDTO(BaseModel):
    period: str
    date: str
    shares: float
    value_nis: float


class RsuIncomeBlockDTO(BaseModel):
    next_12_months_nis: float | None
    quarters: list[RsuQuarterDTO]
    nvda_price_usd: float | None
    fx_usd_nis: float | None
    missing_reasons: list[str]


class EstateExposureBlockDTO(BaseModel):
    us_situs_usd: float | None
    us_situs_nis: float | None
    nra_exemption_usd: float
    above_exemption_usd: float | None
    potential_liability_usd: float | None
    potential_liability_nis: float | None
    missing_reasons: list[str]


class CompositionSliceDTO(BaseModel):
    name: str
    value_nis: float
    pct: float
    holdings: list[str]


class AssumptionsDTO(BaseModel):
    swr_rate: float
    scenario_returns: dict[str, float]
    fx_usd_nis: float | None
    fx_source: str
    current_age: int
    current_age_source: str
    nvda_target_pct: float | None
    nvda_target_source: str | None
    snapshot_date: str | None
    plan_version_id: int | None


class WealthDashboardDTO(BaseModel):
    user_id: str
    generated_at: str
    retirement: RetirementBlockDTO
    cash_runway: CashRunwayBlockDTO
    concentration: ConcentrationBlockDTO
    savings_rate: SavingsRateBlockDTO
    fx_exposure: FxExposureBlockDTO
    rsu_income: RsuIncomeBlockDTO
    estate_exposure: EstateExposureBlockDTO
    asset_class_composition: list[CompositionSliceDTO]
    sector_composition: list[CompositionSliceDTO]
    assumptions: AssumptionsDTO


@router.get("/wealth-dashboard", response_model=WealthDashboardDTO)
def get_wealth_dashboard(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> WealthDashboardDTO:
    """Compute the full /portfolio top-of-page dashboard for ``user_id``.

    See ``argosy.services.wealth_dashboard.compute_wealth_dashboard`` for
    per-block semantics. Each block tolerates missing data: when a
    precondition fails, the relevant fields are ``None`` and the block's
    ``missing_reasons`` carries the human-readable cause.
    """
    dash: WealthDashboard = compute_wealth_dashboard(db, user_id=user_id)
    # asdict round-trip lands us straight in pydantic-validated shape.
    return WealthDashboardDTO(**wealth_dashboard_to_dict(dash))


__all__ = ["router"]
