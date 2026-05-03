"""GET /api/portfolio/snapshot — latest portfolio snapshot for a user.

Phase 2 doesn't yet persist parsed snapshots into a `portfolio_snapshots`
table (that lands with the broker integration in Phase 4). Until then,
this endpoint computes a snapshot on demand from the most recently
ingested TSV file under `${ARGOSY_HOME}/`. If no TSV is found, returns
an empty snapshot — the frontend handles the empty state gracefully.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from argosy.config import get_settings
from argosy.ingest.tsv import parse_portfolio_tsv

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


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


def _find_latest_tsv() -> Path | None:
    """Best-effort: pick the newest .tsv anywhere under ARGOSY_HOME."""
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
    return candidates[0][1]


@router.get("/snapshot", response_model=PortfolioSnapshotDTO)
async def get_portfolio_snapshot(
    user_id: str = Query("ariel"),
) -> PortfolioSnapshotDTO:
    _ = user_id  # multi-tenant slot; Phase 2 ignores it
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


__all__ = ["router"]
