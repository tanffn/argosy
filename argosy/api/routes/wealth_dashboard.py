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
    region_composition: list[CompositionSliceDTO]
    assumptions: AssumptionsDTO


@router.get("/wealth-dashboard", response_model=WealthDashboardDTO)
def get_wealth_dashboard(
    user_id: str = Query("ariel"),
    exclude_nvda: bool = Query(False),
    db: Session = Depends(get_db),
) -> WealthDashboardDTO:
    """Compute the full /portfolio top-of-page dashboard for ``user_id``.

    See ``argosy.services.wealth_dashboard.compute_wealth_dashboard`` for
    per-block semantics. Each block tolerates missing data: when a
    precondition fails, the relevant fields are ``None`` and the block's
    ``missing_reasons`` carries the human-readable cause.
    """
    from datetime import date as _date

    from argosy.services import derived_cache

    def _compute() -> WealthDashboardDTO:
        dash: WealthDashboard = compute_wealth_dashboard(
            db, user_id=user_id, exclude_nvda=exclude_nvda)
        # asdict round-trip lands us straight in pydantic-validated shape.
        return WealthDashboardDTO(**wealth_dashboard_to_dict(dash))

    # Deterministic given (plan, snapshot, exclude_nvda) EXCEPT the cash-runway /
    # RSU-projection blocks anchor on date.today(); fold today's ISO date into the
    # key so a day rollover busts the entry (never serve yesterday's "today").
    version = derived_cache.version_tuple(db, user_id)
    if version is not None:
        version = version + (
            "wealth-dashboard", exclude_nvda, _date.today().isoformat()
        )
    return derived_cache.get_or_compute(
        "portfolio.wealth-dashboard", version, _compute
    )


# ---------------------------------------------------------------------------
# GET /api/portfolio/net-worth-history — the ACTUAL net-worth series from
# portfolio_snapshots history. Read-only; powers the home page's wealth-
# trajectory + deconcentration charts (past-N-months actuals; the projected
# band comes from the wealth-dashboard trajectory above, and the plan glide
# comes from /api/plan/current/allocation-glidepath).
# ---------------------------------------------------------------------------


class NetWorthHistoryPointDTO(BaseModel):
    date: str  # ISO date (snapshot_date, falling back to imported_at's date)
    total_usd: float | None
    #: Direct NVDA position as a % of total book value at that snapshot
    #: (0-100). Direct-position weight, not fund look-through — historical
    #: snapshots carry positions only, so look-through can't be re-derived
    #: retroactively.
    nvda_pct: float | None


class NetWorthHistoryResponseDTO(BaseModel):
    user_id: str
    points: list[NetWorthHistoryPointDTO]


@router.get("/net-worth-history", response_model=NetWorthHistoryResponseDTO)
def get_net_worth_history(
    user_id: str = Query("ariel"),
    months: int = Query(12, ge=1, le=120),
    db: Session = Depends(get_db),
) -> NetWorthHistoryResponseDTO:
    """Chronological per-snapshot net-worth points for the last ``months``.

    One point per calendar date (the freshest import wins when a date was
    re-imported). Rows whose totals can't be parsed yield ``total_usd=None``
    rather than being dropped, so gaps are visible to the caller.
    """
    import json as _json
    from datetime import date as _date, timedelta as _timedelta

    from sqlalchemy import select as _select

    from argosy.state.models import PortfolioSnapshotRow as _SnapRow

    cutoff = _date.today() - _timedelta(days=round(months * 30.44))
    rows = (
        db.execute(
            _select(_SnapRow)
            .where(_SnapRow.user_id == user_id)
            .order_by(_SnapRow.imported_at.asc())
        )
        .scalars()
        .all()
    )

    by_date: dict[str, NetWorthHistoryPointDTO] = {}
    for row in rows:
        d = row.snapshot_date or row.imported_at.date()
        if d < cutoff:
            continue
        total_usd: float | None = None
        nvda_pct: float | None = None
        try:
            totals = _json.loads(row.totals_json or "{}")
            total_k = totals.get("total_usd_value_k")
            if isinstance(total_k, (int, float)):
                total_usd = float(total_k) * 1000.0
                if total_k > 0:
                    positions = _json.loads(row.positions_json or "[]")
                    nvda_k = sum(
                        float(p.get("usd_value_k") or 0.0)
                        for p in positions
                        if str(p.get("symbol") or "").upper() == "NVDA"
                    )
                    nvda_pct = (nvda_k / float(total_k)) * 100.0
        except (ValueError, TypeError):
            pass  # keep the dated point; total_usd/nvda_pct stay None
        # rows iterate oldest-import first, so a later re-import of the same
        # snapshot date overwrites the stale one.
        by_date[d.isoformat()] = NetWorthHistoryPointDTO(
            date=d.isoformat(), total_usd=total_usd, nvda_pct=nvda_pct
        )

    points = [by_date[k] for k in sorted(by_date)]
    return NetWorthHistoryResponseDTO(user_id=user_id, points=points)


__all__ = ["router"]
