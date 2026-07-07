"""Reconstructed net-worth history points from archived TSV exports.

The ``portfolio_snapshots`` table only reaches back to the first ingest
(2026-03-24), but the user's Google Drive Resources folder keeps the
older "Family Finances Status - YY Mon.tsv" exports — full-book
statements (positions + cash + FX) that predate the ingest pipeline.
This module parses those on demand into history points shaped like the
``/api/portfolio/net-worth-history`` payload, each stamped
``reconstructed=True`` + a ``provenance`` string so the UI renders them
visually distinct (hollow/dashed) from real snapshots.

Evidence rules (DO NOT fabricate):

* Only files that parse into a plausible full book are admitted: the
  parse must yield an FX rate AND an NVDA position — the book has held
  both throughout the archive window. The "25 Aug" export predates the
  current TSV layout and mis-parses (28 rows summing to ~$0.4M against
  a real ~$3.3M book, no FX, no NVDA row); it and anything older is
  REJECTED rather than plotted as a fake cliff.
* A point needs a defensible date: the TSV's own snapshot_date header
  when present, else the file's mtime — accepted only when its
  year-month agrees with the filename's "YY Mon" stamp (Google Drive
  re-syncs can touch mtimes; a disagreement means we don't know when
  the export was taken, so no point).
* Reconstructions are NOT snapshots: nothing is persisted to
  ``portfolio_snapshots``. Points are computed here and cached
  in-process keyed on (path, mtime) — invalidation is file-driven, not
  version-driven, which is why this doesn't use the derived_cache.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from argosy.logging import get_logger

log = get_logger(__name__)

_FILENAME_RE = re.compile(
    r"Family Finances Status\s*-\s*(\d{2})\s*([A-Za-z]{3})\.tsv$",
    re.IGNORECASE,
)

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}


@dataclass(frozen=True)
class ReconstructedPoint:
    """One evidence-grade historical net-worth point (TSV-derived)."""

    date: date
    snapshot_date: date | None
    total_usd: float | None
    nvda_pct: float | None
    nvda_usd: float | None
    cash_usd: float | None
    fx_usd_nis: float | None
    total_nis: float | None
    nis_denominated_usd: float | None
    provenance: str


# (resolved path) -> (mtime, point-or-None). None caches a rejection so
# an unparseable file isn't re-read on every request.
_CACHE: dict[str, tuple[float, ReconstructedPoint | None]] = {}


def _candidate_paths() -> list[Path]:
    """Archived TSV exports under ``$ARGOSY_EXPENSE_SAMPLES_ROOT``.

    Only canonical ``Family Finances Status - YY Mon.tsv`` names —
    ``.bak_*`` siblings and the pre-layout ``.csv`` export don't match.
    """
    root_str = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
    if not root_str:
        return []
    root = Path(root_str)
    if not root.exists():
        return []
    try:
        return sorted(
            p for p in root.glob("Family Finances Status*.tsv")
            if _FILENAME_RE.search(p.name)
        )
    except OSError as exc:
        log.warning("net_worth_backfill.walk_failed", error=str(exc))
        return []


def _filename_year_month(name: str) -> tuple[int, int] | None:
    m = _FILENAME_RE.search(name)
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower())
    if month is None:
        return None
    return 2000 + int(m.group(1)), month


def _point_date(path: Path, snap_date: date | None) -> tuple[date, str] | None:
    """The point's date + a provenance suffix, or None when undatable."""
    if snap_date is not None:
        return snap_date, "dated from the TSV header"
    ym = _filename_year_month(path.name)
    if ym is None:
        return None
    try:
        mtime_d = datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return None
    if (mtime_d.year, mtime_d.month) != ym:
        log.info(
            "net_worth_backfill.mtime_filename_mismatch",
            path=path.name, mtime=mtime_d.isoformat(),
        )
        return None
    return mtime_d, "dated from the file mtime"


def _build_point(path: Path) -> ReconstructedPoint | None:
    """Parse one archived TSV into a point; None when not evidence-grade."""
    from argosy.ingest.tsv import parse_portfolio_tsv
    from argosy.services.wealth_dashboard import nvda_concentration_pct

    try:
        snap = parse_portfolio_tsv(path)
    except Exception as exc:  # noqa: BLE001 — a broken archive file is a skip
        log.warning(
            "net_worth_backfill.parse_failed", path=path.name, error=str(exc),
        )
        return None

    dated = _point_date(path, snap.snapshot_date)
    if dated is None:
        return None
    d, date_prov = dated

    positions = [p.model_dump() for p in snap.positions]
    nvda_usd = sum(
        float(p.get("usd_value_k") or 0.0)
        for p in positions
        if str(p.get("symbol") or "").upper() == "NVDA"
    ) * 1000.0
    fx = (
        float(snap.fx_usd_nis)
        if isinstance(snap.fx_usd_nis, (int, float)) and snap.fx_usd_nis > 0
        else None
    )
    # Evidence gate: the archive-era book always carried an FX header and
    # an NVDA position; a parse missing either is a pre-layout export the
    # parser can't read (the "25 Aug" case) — reject, don't plot.
    if fx is None or nvda_usd <= 0 or not positions:
        log.info(
            "net_worth_backfill.rejected_not_evidence_grade",
            path=path.name, fx=fx, nvda_usd=nvda_usd, rows=len(positions),
        )
        return None

    total_usd = sum(
        float(p.get("usd_value_k") or 0.0) for p in positions
    ) * 1000.0
    cash_usd = snap.cash_balances_usd_k() * 1000.0
    nis_denominated_usd = sum(
        float(p.get("usd_value_k") or 0.0)
        for p in positions
        if str(p.get("currency") or "").upper() in ("NIS", "ILS")
    ) * 1000.0
    return ReconstructedPoint(
        date=d,
        snapshot_date=snap.snapshot_date,
        total_usd=total_usd,
        nvda_pct=nvda_concentration_pct(positions),
        nvda_usd=nvda_usd,
        cash_usd=cash_usd,
        fx_usd_nis=fx,
        total_nis=total_usd * fx,
        nis_denominated_usd=nis_denominated_usd,
        provenance=(
            f"reconstructed: archived TSV export ({path.name}, {date_prov})"
        ),
    )


def reconstructed_net_worth_points(
    *, before: date | None = None
) -> list[ReconstructedPoint]:
    """Evidence-grade reconstructed points, oldest first.

    ``before`` bounds the series to dates strictly earlier than the
    first REAL snapshot — reconstructions never compete with actual
    ingested history. Cached per (path, mtime); a changed file re-parses.
    """
    points: list[ReconstructedPoint] = []
    for path in _candidate_paths():
        key = str(path.resolve())
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == mtime:
            point = cached[1]
        else:
            point = _build_point(path)
            _CACHE[key] = (mtime, point)
        if point is None:
            continue
        if before is not None and point.date >= before:
            continue
        points.append(point)
    points.sort(key=lambda p: p.date)
    return points


__all__ = ["ReconstructedPoint", "reconstructed_net_worth_points"]
