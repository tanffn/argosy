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
from argosy.logging import get_logger
from argosy.services.wealth_dashboard import (
    WealthDashboard,
    compute_wealth_dashboard,
    wealth_dashboard_to_dict,
)

logger = get_logger(__name__)

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
    exclude_nvda: bool = False
    estate_safe_usd: float | None = None
    securities_book_usd: float | None = None
    us_situs_pct_of_securities: float | None = None
    estate_safe_pct_of_securities: float | None = None
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
    composition_unavailable_reason: str | None = None


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
    #: ISO date the point is PLOTTED at — the row's PRICE VINTAGE. TSV
    #: rows carry prices as-of their EXPORT time, which can postdate the
    #: stamped snapshot_date (observed: the "Jun 29" TSV exported Jun 30
    #: embedded the Jun-30 close and drew a fake one-day cliff into the
    #: next point); self-refresh rows are priced at import time by
    #: construction. So: ``imported_at``'s date when it differs from
    #: ``snapshot_date``, else ``snapshot_date`` (fallback:
    #: ``imported_at``). Values are NEVER adjusted — the book truly was
    #: worth this at these prices; only the time label moves.
    date: str
    #: The row's stamped snapshot_date (reference; may precede ``date``
    #: when the source TSV was exported later than it was dated). None
    #: when the row carries no snapshot_date.
    snapshot_date: str | None = None
    total_usd: float | None
    #: NVDA % of the TRADEABLE SECURITIES book (0-100) at that snapshot,
    #: via the canonical ``nvda_concentration_pct`` (excludes cash rows and
    #: physical real estate) — the SAME denominator basis as the
    #: TargetAllocationDoc glide, so the deconcentration chart's actual and
    #: glide series join without a fake jump. Direct-position weight, not
    #: fund look-through — historical snapshots carry positions only, so
    #: look-through can't be re-derived retroactively.
    nvda_pct: float | None
    #: Decomposition inputs for the UI's delta tooltip ("why did the book
    #: move between snapshots"): the NVDA position value and the cash
    #: balances at that snapshot, both USD. Consecutive-point deltas of
    #: these attribute a book move to NVDA repricing vs cash flow vs the
    #: rest of the book.
    nvda_usd: float | None = None
    cash_usd: float | None = None
    #: Currency dimension — each point converted at ITS OWN snapshot-date
    #: fx so the ₪ view is the true NIS-perspective wealth (what matters
    #: for FI-in-Israel), not a single-rate rescale of the USD series.
    fx_usd_nis: float | None = None
    total_nis: float | None = None
    #: USD value of the NIS-denominated positions at that snapshot —
    #: the base for the tooltip's explicit FX (translation) component.
    nis_denominated_usd: float | None = None
    #: True for points RECONSTRUCTED from archived TSV exports on disk
    #: (pre-ingest history; see ``argosy.services.net_worth_backfill``).
    #: Never persisted to portfolio_snapshots — the UI renders these
    #: hollow/dashed so a reconstruction is never mistaken for a real
    #: ingested snapshot.
    reconstructed: bool = False
    #: Human-readable evidence trail for reconstructed points, e.g.
    #: "reconstructed: archived TSV export (Family Finances Status -
    #: 25 Oct.tsv, dated from the file mtime)". None on real snapshots.
    provenance: str | None = None


class NetWorthHistoryResponseDTO(BaseModel):
    user_id: str
    points: list[NetWorthHistoryPointDTO]


def _snapshots_stamp(db: Session, user_id: str) -> tuple | None:
    """Staleness stamp over ALL of ``user_id``'s snapshot rows, or None.

    ``version_tuple`` only carries the LATEST snapshot (by snapshot_date), but
    this endpoint renders EVERY row — a backdated re-import (new row with an
    older snapshot_date) changes the series without changing the latest row.
    ``(count, max imported_at)`` bumps on any insert. ``None`` on any DB error
    -> the caller treats the key as uncacheable (always compute).
    """
    try:
        from sqlalchemy import func as _func
        from sqlalchemy import select as _select

        from argosy.state.models import PortfolioSnapshotRow as _SnapRow

        n, max_imported = db.execute(
            _select(
                _func.count(_SnapRow.id), _func.max(_SnapRow.imported_at)
            ).where(_SnapRow.user_id == user_id)
        ).one()
        return (
            int(n or 0),
            max_imported.isoformat()
            if hasattr(max_imported, "isoformat")
            else (str(max_imported) if max_imported is not None else None),
        )
    except Exception:  # noqa: BLE001 — uncacheable beats a stale series
        return None


def _compute_net_worth_history(
    db: Session, user_id: str, months: int
) -> NetWorthHistoryResponseDTO:
    """Build the net-worth-history series (see :func:`get_net_worth_history`).

    Module-level (not a route closure) so the derived-cache warmer can run
    the SAME compute under the SAME key the route reads.
    """
    import json as _json
    from datetime import date as _date, timedelta as _timedelta

    from sqlalchemy import select as _select

    from argosy.services.wealth_dashboard import nvda_concentration_pct
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
        snap_d = row.snapshot_date
        imp_d = row.imported_at.date() if row.imported_at is not None else None
        # Price vintage: imported_at's date when it differs from the
        # stamped snapshot_date (TSV export lag / late ingest), else the
        # snapshot_date itself. Values stay untouched — only the label.
        if snap_d is not None and imp_d is not None and imp_d != snap_d:
            d = imp_d
        else:
            d = snap_d or imp_d
        if d is None or d < cutoff:
            continue
        total_usd: float | None = None
        nvda_pct: float | None = None
        nvda_usd: float | None = None
        cash_usd: float | None = None
        fx: float | None = (
            float(row.fx_usd_nis)
            if isinstance(row.fx_usd_nis, (int, float)) and row.fx_usd_nis > 0
            else None
        )
        nis_denominated_usd: float | None = None
        try:
            totals = _json.loads(row.totals_json or "{}")
            cash_k = totals.get("cash_balances_usd_k")
            if isinstance(cash_k, (int, float)):
                cash_usd = float(cash_k) * 1000.0
            positions = _json.loads(row.positions_json or "[]")
            # BASIS RULE: total = POSITIONS-SUM, never the stored
            # totals_json grand total. History rows mix provenances
            # (TSV ingest / self-refresh reprice / fills-applied) and a
            # stored total can go stale independently of the position
            # rows (the stale-allocations bug class); summing the rows
            # themselves keeps every provenance on one basis. The stored
            # total is only a fallback for rows with no position rows.
            if isinstance(positions, list) and len(positions) > 0:
                total_usd = (
                    sum(
                        float(p.get("usd_value_k") or 0.0)
                        for p in positions
                        if isinstance(p, dict)
                    )
                    * 1000.0
                )
            else:
                total_k = totals.get("total_usd_value_k")
                if isinstance(total_k, (int, float)):
                    total_usd = float(total_k) * 1000.0
            if isinstance(positions, list):
                # Canonical concentration: NVDA ÷ tradeable securities book
                # (excl. cash + physical real estate) — one NVDA weight
                # across surfaces, and the same basis the plan glide uses.
                nvda_pct = nvda_concentration_pct(positions)
                nvda_usd = (
                    sum(
                        float(p.get("usd_value_k") or 0.0)
                        for p in positions
                        if isinstance(p, dict)
                        and str(p.get("symbol") or "").upper() == "NVDA"
                    )
                    * 1000.0
                )
                nis_denominated_usd = (
                    sum(
                        float(p.get("usd_value_k") or 0.0)
                        for p in positions
                        if isinstance(p, dict)
                        and str(p.get("currency") or "").upper()
                        in ("NIS", "ILS")
                    )
                    * 1000.0
                )
        except (ValueError, TypeError):
            pass  # keep the dated point; the value fields stay None
        # rows iterate oldest-import first, so a later re-import of the same
        # snapshot date overwrites the stale one.
        by_date[d.isoformat()] = NetWorthHistoryPointDTO(
            date=d.isoformat(),
            snapshot_date=snap_d.isoformat() if snap_d is not None else None,
            total_usd=total_usd,
            nvda_pct=nvda_pct,
            nvda_usd=nvda_usd,
            cash_usd=cash_usd,
            fx_usd_nis=fx,
            total_nis=(
                total_usd * fx if (total_usd is not None and fx is not None) else None
            ),
            nis_denominated_usd=nis_denominated_usd,
        )

    # Backfill: evidence-grade reconstructed points from the archived
    # on-disk TSV exports, strictly BEFORE the earliest real snapshot
    # (reconstructions never compete with ingested history) and inside
    # the requested window. Computed on demand + mtime-cached in the
    # service; nothing is persisted to portfolio_snapshots.
    try:
        from argosy.services.net_worth_backfill import (
            reconstructed_net_worth_points,
        )

        earliest_real = min(
            (_date.fromisoformat(k) for k in by_date), default=None,
        )
        for rp in reconstructed_net_worth_points(before=earliest_real):
            if rp.date < cutoff:
                continue
            iso = rp.date.isoformat()
            if iso in by_date:
                continue  # a real snapshot always wins the date
            by_date[iso] = NetWorthHistoryPointDTO(
                date=iso,
                snapshot_date=(
                    rp.snapshot_date.isoformat()
                    if rp.snapshot_date is not None else None
                ),
                total_usd=rp.total_usd,
                nvda_pct=rp.nvda_pct,
                nvda_usd=rp.nvda_usd,
                cash_usd=rp.cash_usd,
                fx_usd_nis=rp.fx_usd_nis,
                total_nis=rp.total_nis,
                nis_denominated_usd=rp.nis_denominated_usd,
                reconstructed=True,
                provenance=rp.provenance,
            )
    except Exception:  # noqa: BLE001 — backfill is enrichment, never a 500
        logger.warning("net-worth-history: backfill failed", exc_info=True)

    points = [by_date[k] for k in sorted(by_date)]
    return NetWorthHistoryResponseDTO(user_id=user_id, points=points)


@router.get("/net-worth-history", response_model=NetWorthHistoryResponseDTO)
def get_net_worth_history(
    user_id: str = Query("ariel"),
    months: int = Query(12, ge=1, le=120),
    db: Session = Depends(get_db),
) -> NetWorthHistoryResponseDTO:
    """Chronological per-snapshot net-worth points for the last ``months``.

    One point per calendar date — the row's PRICE VINTAGE (see
    ``NetWorthHistoryPointDTO.date``) — with the freshest import winning
    when a vintage date was re-imported. Rows whose totals can't be
    parsed yield ``total_usd=None`` rather than being dropped, so gaps
    are visible to the caller.

    Memoized in the derived cache (same version-keyed pattern as the
    wealth-dashboard above). The output is a pure function of the user's
    ``portfolio_snapshots`` rows + the archived-TSV backfill files +
    ``date.today()`` (the window cutoff), so the key folds in:

      * ``version_tuple`` — bumps on a new ingested snapshot (id +
        imported_at) and on plan changes (harmless over-invalidation;
        keeps ONE shared staleness anchor across surfaces);
      * ``_snapshots_stamp`` — (count, max imported_at) over ALL the
        user's snapshot rows, because this series reads every row, not
        just the latest (a backdated re-import must bust);
      * ``months`` — distinct windows are distinct entries;
      * today's ISO date — a day rollover moves the cutoff;
      * :func:`backfill_files_fingerprint` — an added/edited archive
        export busts the cached series (file-driven inputs the DB
        version can't see).
    """
    from datetime import date as _date

    from argosy.services import derived_cache
    from argosy.services.net_worth_backfill import backfill_files_fingerprint

    version = derived_cache.version_tuple(db, user_id)
    if version is not None:
        stamp = _snapshots_stamp(db, user_id)
        if stamp is None:
            version = None  # can't stamp the full row set -> don't cache
        else:
            version = version + (
                "net-worth-history",
                months,
                stamp,
                _date.today().isoformat(),
                backfill_files_fingerprint(),
            )
    return derived_cache.get_or_compute(
        "portfolio.net-worth-history",
        version,
        lambda: _compute_net_worth_history(db, user_id, months),
    )


__all__ = ["router"]
