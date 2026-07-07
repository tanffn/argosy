"""Reconstructed net-worth history points from archived TSV exports.

The ``portfolio_snapshots`` table only reaches back to the first ingest
(2026-03-24), but the user's Google Drive Resources folder keeps the
older "Family Finances Status - YY Mon.tsv" exports — full-book
statements (positions + cash + FX) that predate the ingest pipeline.
This module parses those on demand into history points shaped like the
``/api/portfolio/net-worth-history`` payload, each stamped
``reconstructed=True`` + a ``provenance`` string so the UI renders them
visually distinct (hollow/dashed) from real snapshots.

Two archive layouts exist:

* The MODERN layout (Oct 2025 onward) — parsed by the canonical
  ``parse_portfolio_tsv`` (date header row, FX row, sectioned).
* The LEGACY pre-layout sheets ("25 Jul.csv", "25 Aug.tsv") — a
  hand-maintained positions table whose FIRST row is the column header
  (no date row, no FX row), comma- or tab-separated, quoted thousands,
  ad-hoc "sanity check" spill cells in the trailing columns, and NIS
  rows carrying the local value in "Current Value" with the converted
  figure in "(K) USD Value". ``_build_legacy_point`` parses these by
  header-name lookup (never positional), cross-checks the USD rows'
  ``Current Value ≈ (K) USD Value × 1000`` for column alignment, and
  derives FX from the NIS rows' local÷USD ratios (declined when the
  rows disagree by >3% — the point then ships without ₪ fields). The
  physical real-estate row is EXCLUDED from the total: the modern-era
  points carry it at usd_value_k=0 (its value lives in the modern
  real-estate section), so including the legacy row's value would draw
  a fake step into the Oct-2025 point.

Evidence rules (DO NOT fabricate):

* Only files that parse into a plausible full book are admitted: the
  modern parse must yield an FX rate AND an NVDA position — the book
  has held both throughout the archive window; a legacy parse must
  find the header row, ≥10 alignment-checked position rows, and an
  NVDA row. A file failing both parsers is REJECTED with the specific
  reason logged, never plotted as a fake cliff.
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
    r"Family Finances Status\s*-\s*(\d{2})\s*([A-Za-z]{3})\.(?:tsv|csv)$",
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
    """Archived exports under ``$ARGOSY_EXPENSE_SAMPLES_ROOT``.

    Only canonical ``Family Finances Status - YY Mon.<tsv|csv>`` names —
    ``.bak_*`` siblings don't match. The ``.csv`` extension admits the
    oldest pre-layout export ("25 Jul.csv").
    """
    root_str = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
    if not root_str:
        return []
    root = Path(root_str)
    if not root.exists():
        return []
    try:
        return sorted(
            p for p in root.glob("Family Finances Status*.*")
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


# ---------------------------------------------------------------------------
# Legacy pre-layout sheets ("25 Jul.csv", "25 Aug.tsv")
# ---------------------------------------------------------------------------


def _legacy_num(cell: str | None) -> float | None:
    """Parse a legacy numeric cell: quoted thousands, '-' = no value."""
    if cell is None:
        return None
    s = cell.strip().strip('"').replace(",", "").strip()
    if not s or s in {"-", "—"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _legacy_reject(path: Path, reason: str, **extra: object) -> None:
    log.warning(
        "net_worth_backfill.legacy_rejected",
        path=path.name, reason=reason, **extra,
    )


def _build_legacy_point(path: Path) -> ReconstructedPoint | None:
    """Parse a pre-layout export (header-first sheet) into a point.

    Layout quirks handled (both observed variants):
      * comma- OR tab-separated (the "25 Jul" file is a CSV with a
        leading empty column; "25 Aug" is a TSV without one) — columns
        are resolved by HEADER NAME, never by position;
      * quoted thousands ("2,197,875") and CRLF endings;
      * ad-hoc "sanity check" spill cells beyond "% Yearly" (ignored —
        we only read named columns);
      * NIS rows: "Current Value" is the LOCAL amount, "(K) USD Value"
        is converted — FX is derived from those rows' aggregate ratio
        and declined when individual rows disagree by >3% (the USD-K
        column is rounded to whole thousands, so some spread is
        expected; beyond that the rate is not evidence);
      * one CP1255-mojibake Hebrew-ETF symbol row (kept — it is a real
        security; only its display name is garbled);
      * the physical real-estate row is excluded from the total for
        basis consistency with the modern-era points (which carry it
        at usd_value_k=0).
    """
    import csv
    import io

    from argosy.services.wealth_dashboard import nvda_concentration_pct

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _legacy_reject(path, "unreadable", error=str(exc))
        return None
    lines = raw.splitlines()
    if not lines:
        _legacy_reject(path, "empty file")
        return None
    delim = "\t" if "\t" in lines[0] else ","
    rows = list(csv.reader(io.StringIO(raw), delimiter=delim))

    # Find the header row + name→index map (first few rows only).
    header_idx: int | None = None
    col: dict[str, int] = {}
    for i, r in enumerate(rows[:5]):
        lowered = [c.strip().lower() for c in r]
        if "location" in lowered and "(k) usd value" in lowered:
            header_idx = i
            for j, name in enumerate(lowered):
                if name and name not in col:
                    col[name] = j
            break
    if header_idx is None:
        _legacy_reject(path, "no header row (Location + (K) USD Value)")
        return None
    required = ("location", "currency", "type", "symbol",
                "current value", "(k) usd value")
    missing = [n for n in required if n not in col]
    if missing:
        _legacy_reject(path, "header missing columns", missing=missing)
        return None

    def cell(r: list[str], name: str) -> str:
        j = col[name]
        return r[j].strip() if j < len(r) else ""

    positions: list[dict] = []
    real_estate_excluded_k = 0.0
    usd_rows = 0
    usd_rows_aligned = 0
    nis_local_sum = 0.0
    nis_usd_sum = 0.0
    nis_ratios: list[float] = []
    for r in rows[header_idx + 1:]:
        if not any(c.strip() for c in r):
            continue
        location = cell(r, "location")
        usd_k = _legacy_num(cell(r, "(k) usd value"))
        if not location or usd_k is None:
            continue  # spill/junk row — no location or no USD figure
        currency = cell(r, "currency").upper()
        asset_type = cell(r, "type")
        symbol = cell(r, "symbol")
        cv = _legacy_num(cell(r, "current value"))
        if asset_type.lower().startswith("real estate"):
            real_estate_excluded_k += usd_k
            continue
        # Column-alignment cross-check on USD rows: the (K) USD column
        # must be the Current Value in thousands (±1 for rounding).
        if currency == "USD" and cv is not None:
            usd_rows += 1
            if abs(usd_k - cv / 1000.0) <= 1.0:
                usd_rows_aligned += 1
        if currency in ("NIS", "ILS") and cv is not None and usd_k > 0:
            nis_local_sum += cv
            nis_usd_sum += usd_k * 1000.0
            nis_ratios.append(cv / (usd_k * 1000.0))
        positions.append({
            "location": location,
            "currency": currency,
            "asset_type": asset_type,
            "symbol": symbol,
            "usd_value_k": usd_k,
        })

    if len(positions) < 10:
        _legacy_reject(path, "too few position rows", rows=len(positions))
        return None
    if usd_rows == 0 or usd_rows_aligned / usd_rows < 0.8:
        _legacy_reject(
            path, "USD value column misaligned",
            aligned=usd_rows_aligned, usd_rows=usd_rows,
        )
        return None
    nvda_usd = sum(
        p["usd_value_k"] for p in positions
        if str(p["symbol"]).upper() == "NVDA"
    ) * 1000.0
    if nvda_usd <= 0:
        _legacy_reject(path, "no NVDA row")
        return None

    dated = _point_date(path, None)
    if dated is None:
        _legacy_reject(path, "undatable (no header date; mtime unusable)")
        return None
    d, date_prov = dated

    # FX from the NIS rows' local÷USD ratios. The USD-K column is rounded
    # to whole thousands, so require the individual rows to agree with
    # the aggregate within 3% — otherwise the rate is not evidence and
    # the ₪ fields are declined (the USD fields stand on their own).
    fx: float | None = None
    fx_note = "fx undetermined — ₪ fields omitted"
    if nis_usd_sum > 0 and nis_ratios:
        agg = nis_local_sum / nis_usd_sum
        if all(abs(r / agg - 1.0) <= 0.03 for r in nis_ratios):
            fx = agg
            fx_note = "fx derived from NIS-row ratios"
        else:
            log.info(
                "net_worth_backfill.legacy_fx_inconsistent",
                path=path.name, ratios=[round(r, 4) for r in nis_ratios],
            )

    total_usd = sum(p["usd_value_k"] for p in positions) * 1000.0
    cash_usd = sum(
        p["usd_value_k"] for p in positions
        if "cash" in str(p["asset_type"]).lower()
    ) * 1000.0
    nis_denominated_usd = sum(
        p["usd_value_k"] for p in positions
        if p["currency"] in ("NIS", "ILS")
    ) * 1000.0
    point = ReconstructedPoint(
        date=d,
        snapshot_date=None,
        total_usd=total_usd,
        nvda_pct=nvda_concentration_pct(positions),
        nvda_usd=nvda_usd,
        cash_usd=cash_usd,
        fx_usd_nis=fx,
        total_nis=total_usd * fx if fx is not None else None,
        nis_denominated_usd=nis_denominated_usd,
        provenance=(
            f"legacy-tsv:{path.name} — pre-layout export, {date_prov}; "
            f"{fx_note}; real-estate row excluded for basis consistency"
        ),
    )
    log.info(
        "net_worth_backfill.legacy_point_built",
        path=path.name, date=d.isoformat(),
        total_usd=round(total_usd), nvda_pct=round(point.nvda_pct or 0, 1),
        fx=(round(fx, 4) if fx is not None else None),
        real_estate_excluded_k=real_estate_excluded_k,
    )
    return point


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
            # Modern layout first (the canonical parser); the legacy
            # pre-layout adapter only sees files the modern gate rejects
            # (legacy sheets carry no FX header row, so they always do).
            point = _build_point(path) or _build_legacy_point(path)
            _CACHE[key] = (mtime, point)
        if point is None:
            continue
        if before is not None and point.date >= before:
            continue
        points.append(point)
    points.sort(key=lambda p: p.date)
    return points


__all__ = ["ReconstructedPoint", "reconstructed_net_worth_points"]
