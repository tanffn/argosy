"""GET /api/portfolio/snapshot — latest portfolio snapshot for a user.

T1.5 call-site rewiring: this route now prefers the DB-backed
``portfolio_snapshots`` table when a row exists for the user; the
filesystem walk + TSV parse is the fallback path. On a fallback, the
route also write-throughs the parsed snapshot into the DB so subsequent
requests serve from the DB (idempotent — see
``portfolio_snapshot_store.write_through_if_changed``).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.config import get_settings
from argosy.ingest.tsv import parse_portfolio_tsv
from argosy.logging import get_logger
from argosy.services.portfolio_snapshot_store import (
    get_latest_snapshot_row,
    row_to_snapshot,
    write_through_if_changed,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])
_log = get_logger(__name__)


class PositionDTO(BaseModel):
    location: str
    currency: str
    asset_type: str
    details: str
    symbol: str
    shares: float | None
    current_price: float | None
    usd_value_k: float | None


class AllocationDTO(BaseModel):
    category: str
    pct: float | None
    target_pct: float | None
    delta_k: float | None


class PortfolioSnapshotDTO(BaseModel):
    snapshot_date: str | None
    fx_usd_nis: float | None
    fx_usd_eur: float | None
    total_usd_value_k: float
    positions: list[PositionDTO]
    allocations: list[AllocationDTO]
    source_path: str | None
    parse_warnings: list[str]


_PORTFOLIO_TSV_HEADER_MARKER = "Bank account / funds allocation"


def _find_latest_tsv() -> Path | None:
    """Return the newest portfolio TSV under ARGOSY_HOME or None.

    Filters by the presence of the ``"Bank account / funds allocation"``
    header marker so stray small uploads (e.g. attachment placeholders
    under ``uploads/<user>/.../<timestamp>__<hash>__p.tsv``) don't shadow
    the real ``Family Finances Status - <date>.tsv`` file.
    """
    settings = get_settings()
    home = settings.home
    candidates: list[tuple[float, Path]] = []
    for tsv in home.rglob("*.tsv"):
        # Skip our own scratch / temp directories.
        s = str(tsv).lower()
        if any(seg in s for seg in (".venv", "node_modules", "__pycache__")):
            continue
        try:
            mtime = tsv.stat().st_mtime
        except OSError:  # pragma: no cover - defensive
            continue
        candidates.append((mtime, tsv))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    for _, path in candidates:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                head = f.read(4096)
        except OSError:  # pragma: no cover - defensive
            continue
        if _PORTFOLIO_TSV_HEADER_MARKER in head:
            return path
    return None


def _snapshot_to_dto(snap) -> PortfolioSnapshotDTO:
    """Translate a parsed/hydrated PortfolioSnapshot to the route DTO."""
    positions: list[PositionDTO] = []
    for p in snap.positions:
        positions.append(
            PositionDTO(
                location=p.location,
                currency=p.currency,
                asset_type=p.asset_type,
                details=p.details,
                symbol=p.symbol,
                shares=p.shares,
                current_price=p.current_price,
                usd_value_k=p.usd_value_k,
            )
        )
    allocations: list[AllocationDTO] = []
    for a in snap.allocations:
        allocations.append(
            AllocationDTO(
                category=a.category,
                pct=a.pct,
                target_pct=a.target_pct,
                delta_k=a.delta_k,
            )
        )
    return PortfolioSnapshotDTO(
        snapshot_date=snap.snapshot_date.isoformat() if snap.snapshot_date else None,
        fx_usd_nis=snap.fx_usd_nis,
        fx_usd_eur=snap.fx_usd_eur,
        total_usd_value_k=snap.total_usd_value_k,
        positions=positions,
        allocations=allocations,
        source_path=snap.source_path,
        parse_warnings=snap.parse_warnings,
    )


@router.get("/snapshot", response_model=PortfolioSnapshotDTO)
def get_portfolio_snapshot(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> PortfolioSnapshotDTO:
    """Return the latest portfolio snapshot for ``user_id``.

    T1.5 lookup order:
      1. Prefer the most recent ``portfolio_snapshots`` row for the user.
      2. Fallback: walk ``ARGOSY_HOME`` for the freshest TSV with the
         canonical header marker, parse it, write-through into the DB
         (idempotent — same source_path + date + size = no-op), and
         serve the parsed result.
      3. Empty DTO when neither path yields data.
    """
    # 1. DB-first.
    try:
        row = get_latest_snapshot_row(db, user_id)
    except Exception as exc:  # noqa: BLE001 - defensive
        _log.warning(
            "portfolio_snapshot.db_lookup_failed",
            user_id=user_id, error=str(exc),
        )
        row = None
    if row is not None:
        try:
            snap = row_to_snapshot(row)
            return _snapshot_to_dto(snap)
        except Exception as exc:  # noqa: BLE001 - defensive
            _log.warning(
                "portfolio_snapshot.db_hydrate_failed",
                user_id=user_id, row_id=row.id, error=str(exc),
            )
            # Fall through to filesystem walk.

    # 2. Filesystem fallback + write-through.
    tsv = _find_latest_tsv()
    if tsv is None:
        return PortfolioSnapshotDTO(
            snapshot_date=None,
            fx_usd_nis=None,
            fx_usd_eur=None,
            total_usd_value_k=0.0,
            positions=[],
            allocations=[],
            source_path=None,
            parse_warnings=["No TSV found under ARGOSY_HOME."],
        )

    snap = parse_portfolio_tsv(tsv)
    try:
        write_through_if_changed(db, user_id=user_id, snapshot=snap)
    except Exception as exc:  # noqa: BLE001 - defensive
        _log.warning(
            "portfolio_snapshot.write_through_failed",
            user_id=user_id, error=str(exc),
        )
    return _snapshot_to_dto(snap)


__all__ = ["router"]
