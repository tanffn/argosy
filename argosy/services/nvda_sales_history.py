"""NVDA YTD sales accounting — sourced from fills with TSV fallback.

Why this exists
---------------
Run-#26 era investigation: ``Phase1Inputs.nvda_shares_sold_ytd`` was declared
on the dataclass but never populated, so ``ConcentrationAnalystAgent``
emitted ``NvdaPace.shares_sold_ytd=0`` in every synthesis report, and the
home page widget read "0 / 10,000 shares sold YTD · BEHIND PACE" forever.

Two real sources exist for past NVDA sales:

1. The ``fills`` table — populated by ``argosy.services.schwab_lots_ingest``
   when the user runs ``argosy ingest schwab-lots <csv>``. Empty in dev so
   far; this is the canonical source once Schwab CSVs land.
2. The Family Finances Status TSV — parsed live via
   ``argosy.ingest.tsv.parse_portfolio_tsv``, exposes a ``nvda_sales``
   block with ``{month, shares, price}`` entries. Month-only (no exact
   date), so the YTD filter uses the snapshot's anchor year.

This module is a single thin helper that returns the YTD shares-sold count
preferring ``fills`` and falling back to the TSV. Both branches degrade to
0 with a structured log when their source is unreachable so synthesis never
crashes on missing data.

Also exposes ``compute_nvda_target_shares_ytd`` — reads the active draft's
``horizon_medium_json`` for the NVDA-sale annual target (label contains
``NVDA`` + ``unit == 'shares'``) and pro-rates by ``days_elapsed / 365``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger

log = get_logger(__name__)


_NVDA_TICKER = "NVDA"


def _today(as_of: date | None) -> date:
    """Resolve the "today" anchor; defaults to UTC date when None."""
    if as_of is not None:
        return as_of
    return datetime.now(timezone.utc).date()


def _start_of_year(d: date) -> date:
    return date(d.year, 1, 1)


def _shares_sold_from_fills(
    session: Session, user_id: str, *, year_start: date, as_of: date
) -> int | None:
    """Sum NVDA sells from the ``fills`` table between ``year_start`` and ``as_of``.

    Returns the integer total when there is at least one NVDA fill in the
    table for ``user_id`` (so the table-is-the-source-of-truth wins over
    the TSV fallback). Returns ``None`` when the table has no NVDA rows
    at all for this user — that's the cue for the caller to fall back to
    the TSV-derived sales block.

    A "sell" is recognised by either:
      * ``action`` upper-cased starting with ``SELL`` / ``SOLD`` / ``S``, OR
      * ``quantity < 0`` regardless of the action string.

    Quantity is taken as ``abs(quantity)`` for sells so the convention
    (negative-qty vs SELL action) doesn't change the total.
    """
    from argosy.state.models import Fill

    try:
        rows = list(
            session.execute(
                select(Fill).where(
                    Fill.user_id == user_id,
                    Fill.ticker == _NVDA_TICKER,
                )
            ).scalars()
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "nvda_sales_history.fills_query_failed",
            user_id=user_id, error=str(exc),
        )
        return None

    if not rows:
        return None

    start_dt = datetime(year_start.year, year_start.month, year_start.day, tzinfo=timezone.utc)
    end_dt = datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59, tzinfo=timezone.utc)

    total = 0.0
    for r in rows:
        filled_at = r.filled_at
        if filled_at is None:
            continue
        # SQLite stores naive datetimes; normalise to UTC for the compare.
        if filled_at.tzinfo is None:
            filled_at = filled_at.replace(tzinfo=timezone.utc)
        if filled_at < start_dt or filled_at > end_dt:
            continue
        action = (r.action or "").strip().upper()
        qty = float(r.quantity or 0)
        is_sell = qty < 0 or action.startswith("SELL") or action.startswith("SOLD")
        # Be explicit: a BUY with positive qty is excluded.
        if action.startswith("BUY") and qty >= 0:
            is_sell = False
        if not is_sell:
            continue
        total += abs(qty)
    return int(round(total))


_MONTH_MAP: dict[str, int] | None = None


def _month_index(name: str) -> int | None:
    """Map ``"Jan"`` / ``"January"`` / ``"jan"`` -> 1..12 (None on unknown)."""
    global _MONTH_MAP
    if _MONTH_MAP is None:
        from calendar import month_abbr, month_name

        _MONTH_MAP = {m.lower(): i for i, m in enumerate(month_name) if m}
        _MONTH_MAP.update({m.lower(): i for i, m in enumerate(month_abbr) if m})
    return _MONTH_MAP.get((name or "").strip().lower())


def _shares_sold_from_tsv(*, as_of: date) -> int:
    """Fallback: parse the latest Family Finances Status TSV, sum YTD
    ``nvda_sales`` rows whose month resolves into the current year.

    Returns 0 when the TSV is unreachable / unparseable / has no NVDA
    sales block. Dedups on ``(month, shares)`` because the TSV
    occasionally repeats the same row (observed in run #25's snapshot).
    """
    try:
        from argosy.api.routes.portfolio import _find_latest_tsv
        from argosy.ingest.tsv import parse_portfolio_tsv

        tsv = _find_latest_tsv()
        if tsv is None:
            log.info("nvda_sales_history.tsv_fallback_no_tsv")
            return 0
        snap = parse_portfolio_tsv(tsv)
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "nvda_sales_history.tsv_fallback_failed", error=str(exc),
        )
        return 0

    sales = getattr(snap, "nvda_sales", None) or []
    if not sales:
        return 0

    # Anchor year: the snapshot_date when available, else as_of.year.
    snap_date = getattr(snap, "snapshot_date", None)
    anchor_year = snap_date.year if snap_date is not None else as_of.year

    seen: set[tuple[str, int]] = set()
    total = 0
    for s in sales:
        month = getattr(s, "month", None)
        shares = getattr(s, "shares", None)
        if not month or not shares:
            continue
        m_idx = _month_index(month)
        if m_idx is None:
            continue
        try:
            shares_int = int(shares)
        except (TypeError, ValueError):
            continue
        if shares_int <= 0:
            continue
        # Stay within the YTD window for as_of's year. If the TSV was
        # captured in the current year, every entry up to and including
        # the as_of month counts. If the TSV is from a prior year, this
        # branch shouldn't fire — fall through to 0.
        if anchor_year != as_of.year:
            continue
        if m_idx > as_of.month:
            continue
        key = (month.strip().lower(), shares_int)
        if key in seen:
            continue
        seen.add(key)
        total += shares_int
    return total


def compute_nvda_shares_sold_ytd(
    session: Session, user_id: str, *, as_of: date | None = None
) -> int:
    """Return YTD shares sold for NVDA (Jan 1 .. as_of, inclusive).

    Source priority:
      1. ``fills`` table when it has at least one NVDA row for ``user_id``.
      2. Fallback to the latest Family Finances Status TSV's
         ``nvda_sales`` block. (Month-granular; current-year only.)

    Returns 0 when neither source produces data (the caller surfaces that
    as "no fills found" rather than crashing synthesis).
    """
    today = _today(as_of)
    year_start = _start_of_year(today)

    fills_total = _shares_sold_from_fills(
        session, user_id, year_start=year_start, as_of=today
    )
    if fills_total is not None:
        log.info(
            "nvda_sales_history.shares_sold_ytd_from_fills",
            user_id=user_id, total=fills_total,
        )
        return fills_total

    tsv_total = _shares_sold_from_tsv(as_of=today)
    log.info(
        "nvda_sales_history.shares_sold_ytd_from_tsv",
        user_id=user_id, total=tsv_total,
    )
    return tsv_total


def _annual_nvda_target_from_plan(plan_version: Any | None) -> int:
    """Read the annual NVDA-sale target (shares) from a draft's horizons.

    Scans ``horizon_medium_json`` first (12-month deconcentration target
    lives there), then ``horizon_long_json`` as a backstop. Matches target
    rows whose ``label`` mentions ``NVDA`` AND whose ``unit`` contains
    ``shares``. Returns 0 when nothing matches.

    Disambiguation when multiple candidates exist:

      * Prefer labels mentioning ``sell`` / ``deconcentrat`` / ``reduc`` —
        these are flow targets (the actual planned sales count).
      * Skip labels mentioning ``ceiling`` / ``ending`` / ``cap`` —
        these are stock targets (target ending share count), not flow.

    Live-DB shape today (draft #10): two ``shares``-unit rows exist —
    "NVDA deconcentration shares to sell (next 12 months, ...)" and
    "NVDA ending share-count at 12-month gate (reconciled)". The first
    is the flow, the second is the stock; without this disambiguation
    the first-match heuristic would silently pick whichever came first
    in the JSON.
    """
    if plan_version is None:
        return 0
    for json_attr in ("horizon_medium_json", "horizon_long_json"):
        raw = getattr(plan_version, json_attr, None)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        targets = payload.get("targets") if isinstance(payload, dict) else None
        if not isinstance(targets, list):
            continue

        flow_match: int = 0
        any_match: int = 0
        for t in targets:
            if not isinstance(t, dict):
                continue
            label = (t.get("label") or "")
            unit = (t.get("unit") or "")
            label_l = label.lower()
            if "NVDA" not in label.upper():
                continue
            if "shares" not in unit.lower():
                continue
            # Skip explicit stock-target labels (ceiling / ending / cap).
            if any(tok in label_l for tok in ("ceiling", "ending", " cap ", "cap-", "cap)")):
                continue
            val = t.get("value")
            try:
                ival = int(round(float(val)))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if ival <= 0:
                continue
            # Prefer a flow keyword. First flow match wins.
            if any(tok in label_l for tok in ("sell", "deconcentrat", "reduc", "trim", "sale")):
                flow_match = ival
                break
            if any_match == 0:
                any_match = ival
        if flow_match:
            return flow_match
        if any_match:
            return any_match
    return 0


def compute_nvda_target_shares_ytd(
    session: Session, user_id: str, *, as_of: date | None = None
) -> int:
    """Pro-rated YTD target shares for NVDA sales.

    Reads the active draft plan's annual NVDA-sale target (shares) and
    multiplies by ``days_elapsed / 365``. Returns 0 when no draft / no
    NVDA target is found — the home widget treats 0 as "no plan target
    yet" and renders a neutral badge.

    Falls back to the current-accepted plan when no draft exists.
    """
    today = _today(as_of)
    days_elapsed = (today - _start_of_year(today)).days + 1  # inclusive of today

    try:
        from argosy.state.queries import get_current_plan, get_pending_draft

        pv = get_pending_draft(session, user_id) or get_current_plan(session, user_id)
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "nvda_sales_history.plan_lookup_failed",
            user_id=user_id, error=str(exc),
        )
        return 0

    annual = _annual_nvda_target_from_plan(pv)
    if annual <= 0:
        log.info(
            "nvda_sales_history.no_annual_nvda_target", user_id=user_id,
        )
        return 0

    target = int(round(annual * (days_elapsed / 365.0)))
    log.info(
        "nvda_sales_history.target_shares_ytd",
        user_id=user_id, annual=annual,
        days_elapsed=days_elapsed, target=target,
    )
    return target


__all__ = [
    "compute_nvda_shares_sold_ytd",
    "compute_nvda_target_shares_ytd",
]
