"""NVDA YTD sales accounting — sourced from fills with TSV fallback.

Why this exists
---------------
Run-#26 era investigation: ``Phase1Inputs.nvda_shares_sold_ytd`` was declared
on the dataclass but never populated, so ``ConcentrationAnalystAgent``
emitted ``NvdaPace.shares_sold_ytd=0`` in every synthesis report, and the
home page widget read "0 / 10,000 shares sold YTD · BEHIND PACE" forever.

Sale sources, in priority order (binding rule: the Schwab Equity Awards
CSV is the ONLY real-sale source; everything else is derived/secondary):

1. Schwab Equity Awards Center CSVs on disk under
   ``$ARGOSY_EXPENSE_SAMPLES_ROOT/<year>/Schwab/*.csv`` — parsed live via
   ``argosy.services.rsu_reconciliation.schwab_csv.parse_csv``. Per-sale
   EXACT dates (the month-granular TSV rows can't window a tax year
   correctly). Parsed on demand, never persisted — no new ingestion
   framework.
2. The ``fills`` table — broker fills reconciled by the deploy loop.
3. The latest portfolio snapshot's ``nvda_sales_json`` (month-granular).
4. The Family Finances Status TSV ``nvda_sales`` block (month-granular,
   OUTPUT-only artifact — last resort).

All branches degrade with a structured log when their source is
unreachable so synthesis never crashes on missing data.

Also exposes the ONE canonical NVDA sale-flow derivation,
``compute_nvda_sale_pace``. The NVDA sell-down is managed per CALENDAR
TAX YEAR (Israeli CGT is assessed Jan–Dec): the headline is the tax-year
quota — the glide's implied NVDA weight at Dec 31, converted to shares
via the held-at-plan-start count, anchored to the ACTUAL Jan-1 holdings
(reconstructed as held-now + sold-this-calendar-year) so a mid-year plan
revision never resets the year and pre-plan sales count toward the quota.
The next dated glide waypoint is surfaced as the secondary checkpoint.
``compute_nvda_target_shares_ytd`` is a thin back-compat wrapper over it.
The legacy medium-horizon ``shares``-unit target + calendar-YTD proration
survives only as the fallback when a plan carries no glide doc.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger

log = get_logger(__name__)


_NVDA_TICKER = "NVDA"


def _schwab_csv_paths() -> list[Path]:
    """All Schwab Equity Awards CSVs under
    ``$ARGOSY_EXPENSE_SAMPLES_ROOT/<year>/Schwab/*.csv``.

    Same walk as ``/api/expenses/rsu-reconciliation`` (any directory name
    is accepted at the year level so ad-hoc names like ``archive`` work).
    Empty list when the env var is unset / the root doesn't exist.
    """
    import os

    root_str = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
    if not root_str:
        return []
    root = Path(root_str)
    if not root.exists():
        return []
    paths: list[Path] = []
    try:
        for year_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            schwab_dir = year_dir / "Schwab"
            if not schwab_dir.is_dir():
                continue
            paths.extend(sorted(schwab_dir.glob("*.csv")))
    except OSError as exc:
        log.warning("nvda_sales_history.schwab_walk_failed", error=str(exc))
        return []
    return paths


def _shares_sold_from_schwab_csv(
    *, year_start: date, as_of: date
) -> int | None:
    """Sum NVDA sale shares from the on-disk Schwab CSVs (EXACT dates).

    The Schwab Equity Awards CSV is the binding real-sale source — when
    it carries at least one NVDA Sale row for this user's account, it
    outranks every derived source (fills / snapshot / TSV). Returns
    ``None`` when no CSVs exist or none carries an NVDA sale — the cue
    to fall through. Dedups sales across overlapping exports on
    ``(date, symbol, quantity, gross, fees)`` — the same key the
    rsu-reconciliation endpoint uses.
    """
    paths = _schwab_csv_paths()
    if not paths:
        return None
    try:
        from argosy.services.rsu_reconciliation.schwab_csv import parse_csv
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning("nvda_sales_history.schwab_import_failed", error=str(exc))
        return None

    seen: set[tuple] = set()
    any_nvda_sale = False
    total = 0
    for p in paths:
        try:
            report = parse_csv(p)
        except Exception as exc:  # noqa: BLE001 — skip unreadable CSVs
            log.warning(
                "nvda_sales_history.schwab_parse_failed",
                path=str(p), error=str(exc),
            )
            continue
        for sale in report.sales:
            if (sale.symbol or "").strip().upper() != _NVDA_TICKER:
                continue
            if sale.date is None:
                continue
            key = (
                sale.date, sale.symbol, sale.quantity_shares,
                round(sale.gross_usd, 2), round(sale.fees_usd, 2),
            )
            if key in seen:
                continue
            seen.add(key)
            any_nvda_sale = True
            if year_start <= sale.date <= as_of:
                total += int(sale.quantity_shares or 0)
    if not any_nvda_sale:
        return None
    return total


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


def _sum_monthly_sales(
    sales: list[Any], *, anchor_year: int, as_of: date, since: date | None = None
) -> int:
    """Sum month-granular sale rows for the ``[since or Jan 1 .. as_of]`` window.

    Shared by the snapshot- and TSV-sourced branches. Rows may be dicts
    (snapshot ``nvda_sales_json``) or objects (parsed TSV). Dedups on the
    IDENTICAL ``(month, shares, price)`` tuple — the source occasionally
    repeats a row verbatim (observed in run #25's snapshot and again in
    the dev snapshots' double "Apr 520 @ 199.56" row). Including the
    price keeps two GENUINE same-size sales in one month (different
    prices) both counted instead of silently collapsing them.

    ``since`` filters at MONTH granularity (the source has no day): a row in
    ``since``'s own month counts as inside the window — the approximation is
    at most one month wide and only on the window's first month.
    """
    def _get(s: Any, key: str) -> Any:
        if isinstance(s, dict):
            return s.get(key)
        return getattr(s, key, None)

    seen: set[tuple[str, int, float | None]] = set()
    total = 0
    for s in sales:
        month = _get(s, "month")
        shares = _get(s, "shares")
        price = _get(s, "price")
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
        if anchor_year != as_of.year:
            continue
        if m_idx > as_of.month:
            continue
        if since is not None:
            if since.year > anchor_year:
                continue
            if since.year == anchor_year and m_idx < since.month:
                continue
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        key = (str(month).strip().lower(), shares_int, price_f)
        if key in seen:
            continue
        seen.add(key)
        total += shares_int
    return total


def _shares_sold_from_snapshot(
    session: Session, user_id: str, *, as_of: date, since: date | None = None
) -> int | None:
    """YTD NVDA sales from the latest portfolio snapshot's ``nvda_sales_json``.

    The snapshot is internal state (the TSV is OUTPUT-only), so it outranks
    the on-disk TSV fallback. Returns ``None`` when there is no snapshot or
    the snapshot carries no sales block — the cue to fall through to the TSV.
    """
    try:
        from argosy.services.portfolio_snapshot_store import (
            get_latest_snapshot_row,
        )

        row = get_latest_snapshot_row(session, user_id)
        if row is None or not getattr(row, "nvda_sales_json", None):
            return None
        sales = json.loads(row.nvda_sales_json)
        if not isinstance(sales, list) or not sales:
            return None
        snap_date = getattr(row, "snapshot_date", None)
        anchor_year = snap_date.year if snap_date is not None else as_of.year
        return _sum_monthly_sales(
            sales, anchor_year=anchor_year, as_of=as_of, since=since,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "nvda_sales_history.snapshot_fallback_failed",
            user_id=user_id, error=str(exc),
        )
        return None


def _shares_sold_from_tsv(*, as_of: date, since: date | None = None) -> int:
    """Fallback: parse the latest Family Finances Status TSV, sum YTD
    ``nvda_sales`` rows whose month resolves into the current year.

    Returns 0 when the TSV is unreachable / unparseable / has no NVDA
    sales block. Dedups identical ``(month, shares, price)`` rows because
    the TSV occasionally repeats the same row verbatim (observed in run
    #25's snapshot and the May/Jun-2026 exports' double Apr row).
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
    return _sum_monthly_sales(
        sales, anchor_year=anchor_year, as_of=as_of, since=since,
    )


def compute_nvda_shares_sold_ytd(
    session: Session,
    user_id: str,
    *,
    as_of: date | None = None,
    since: date | None = None,
) -> int:
    """Return shares sold for NVDA in ``[since or Jan 1 .. as_of]`` inclusive.

    ``since=None`` keeps the historical calendar-YTD semantics; the
    pace derivation (``compute_nvda_sale_pace``) also passes the plan's
    start date for the plan-window count.

    Source priority:
      1. The on-disk Schwab Equity Awards CSVs — the binding real-sale
         source, per-sale EXACT dates. Wins whenever at least one NVDA
         Sale row exists across the CSVs.
      2. ``fills`` table when it has at least one NVDA row for ``user_id``.
      3. The latest portfolio snapshot's ``nvda_sales_json`` block —
         internal state (the TSV is OUTPUT-only). Month-granular.
      4. Fallback to the latest Family Finances Status TSV's
         ``nvda_sales`` block. (Month-granular; current-year only.)

    Returns 0 when no source produces data (the caller surfaces that
    as "no fills found" rather than crashing synthesis).
    """
    today = _today(as_of)
    window_start = since or _start_of_year(today)

    schwab_total = _shares_sold_from_schwab_csv(
        year_start=window_start, as_of=today
    )
    if schwab_total is not None:
        log.info(
            "nvda_sales_history.shares_sold_from_schwab_csv",
            user_id=user_id, total=schwab_total,
        )
        return schwab_total

    fills_total = _shares_sold_from_fills(
        session, user_id, year_start=window_start, as_of=today
    )
    if fills_total is not None:
        log.info(
            "nvda_sales_history.shares_sold_ytd_from_fills",
            user_id=user_id, total=fills_total,
        )
        return fills_total

    snap_total = _shares_sold_from_snapshot(
        session, user_id, as_of=today, since=since,
    )
    if snap_total is not None:
        log.info(
            "nvda_sales_history.shares_sold_ytd_from_snapshot",
            user_id=user_id, total=snap_total,
        )
        return snap_total

    tsv_total = _shares_sold_from_tsv(as_of=today, since=since)
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


def _nvda_glide_weights(plan_version: Any | None) -> list[tuple[date, float]] | None:
    """Extract the NVDA weight per glide waypoint from the plan's
    ``TargetAllocationDoc`` (``target_allocation_json``).

    The NVDA class is identified STRUCTURALLY — the class whose
    ``instruments`` include the NVDA symbol — never by label substring
    ("Global quality growth (ex-NVDA-dense)" also mentions NVDA).
    Returns ``[(waypoint_date, weight_pct), ...]`` sorted by date, or
    ``None`` when the plan carries no glide doc / no NVDA class.
    """
    if plan_version is None:
        return None
    raw = getattr(plan_version, "target_allocation_json", None)
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None

    nvda_label: str | None = None
    for c in doc.get("classes") or []:
        if not isinstance(c, dict):
            continue
        for inst in c.get("instruments") or []:
            sym = (inst.get("symbol") if isinstance(inst, dict) else None) or ""
            if sym.strip().upper() == _NVDA_TICKER:
                nvda_label = c.get("label")
                break
        if nvda_label:
            break
    if not nvda_label:
        return None

    points: list[tuple[date, float]] = []
    for w in doc.get("glide") or []:
        if not isinstance(w, dict):
            continue
        d_raw = w.get("date")
        comp = w.get("composition_pct_by_class")
        if not d_raw or not isinstance(comp, dict) or nvda_label not in comp:
            continue
        try:
            d = date.fromisoformat(str(d_raw))
            weight = float(comp[nvda_label])
        except (TypeError, ValueError):
            continue
        points.append((d, weight))
    points.sort(key=lambda p: p[0])
    return points if len(points) >= 2 else None


def _interp_weight(points: list[tuple[date, float]], at: date) -> float:
    """Linear interpolation of the glide weight at ``at`` (clamped at the ends)."""
    if at <= points[0][0]:
        return points[0][1]
    if at >= points[-1][0]:
        return points[-1][1]
    for (d0, w0), (d1, w1) in zip(points, points[1:]):
        if d0 <= at <= d1:
            span = (d1 - d0).days
            if span <= 0:
                return w1
            frac = (at - d0).days / span
            return w0 + (w1 - w0) * frac
    return points[-1][1]


def _nvda_shares_held_now(session: Session, user_id: str) -> float:
    """Total NVDA shares in the latest portfolio snapshot (0.0 when absent)."""
    try:
        from argosy.services.portfolio_snapshot_store import (
            get_latest_snapshot_row,
        )

        row = get_latest_snapshot_row(session, user_id)
        if row is None or not getattr(row, "positions_json", None):
            return 0.0
        total = 0.0
        for p in json.loads(row.positions_json) or []:
            if not isinstance(p, dict):
                continue
            if (p.get("symbol") or "").strip().upper() != _NVDA_TICKER:
                continue
            total += float(p.get("shares") or 0.0)
        return total
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "nvda_sales_history.held_now_lookup_failed",
            user_id=user_id, error=str(exc),
        )
        return 0.0


@dataclass(frozen=True)
class NvdaSalePace:
    """The ONE canonical NVDA sale-flow pace — TAX-YEAR framed.

    The sell-down is managed per CALENDAR TAX YEAR (Israeli CGT is
    assessed Jan–Dec), so on ``basis="glide"``:

      * ``annual_flow`` — the tax-year QUOTA: shares to sell between
        Jan 1 and Dec 31 of ``tax_year``. Derived as actual-Jan-1
        holdings (held-now + sold-this-calendar-year) minus the glide's
        implied holdings at Dec 31 (``S0 * w(Dec31)/w(start)``). Anchoring
        to the ACTUAL Jan-1 book means a mid-year plan revision never
        resets the year and pre-plan sales count toward the quota.
      * ``target_shares`` — the schedule-implied sold-by-now expectation
        (actual-Jan-1 holdings minus the glide's implied holdings today).
        The glide IS the schedule through the tax year; a linear daily
        pro-rata was the "0/27 by day 2" noise this replaces.
      * ``sold_shares`` — CALENDAR-year sales (all sources; pre-plan
        sales included — they count toward the tax-year quota). Equals
        ``sold_calendar_ytd`` on this basis.
      * ``next_waypoint_*`` — the next dated glide checkpoint and the
        shares left to sell (from current holdings) to hit its weight.

    ``basis="horizon"``: legacy fallback (no glide doc on the plan) —
    the medium-horizon ``shares``-unit annual target pro-rated
    calendar-YTD; waypoint fields stay empty.

    ``basis="none"``: no plan / no NVDA flow target anywhere.
    """

    target_shares: int = 0        # shares the plan expects sold by as_of
    sold_shares: int = 0          # shares actually sold in the pace window
    annual_flow: int = 0          # tax-year quota (glide) / annual target (horizon)
    plan_start: date | None = None
    sold_calendar_ytd: int = 0
    basis: str = "none"
    tax_year: int | None = None
    sold_since_plan_start: int = 0
    next_waypoint_date: date | None = None
    next_waypoint_weight_pct: float | None = None
    shares_to_sell_by_waypoint: int = 0

    @property
    def delta_shares(self) -> int:
        return self.sold_shares - self.target_shares

    @property
    def tolerance_shares(self) -> int:
        """Pace band: ±10% of the tax-year quota, min 25 shares.

        Deliberately generous — the waypoints are quarterly commitments,
        not daily quotas; banding against the to-date expectation would
        shrink to nothing early in a segment and flag day-1 noise.
        """
        return max(25, int(round(0.10 * self.annual_flow)))

    @property
    def status(self) -> str:
        if self.delta_shares > self.tolerance_shares:
            return "ahead"
        if self.delta_shares < -self.tolerance_shares:
            return "behind"
        return "on"

    @property
    def on_track(self) -> bool:
        return self.delta_shares >= -self.tolerance_shares


def compute_nvda_sale_pace(
    session: Session, user_id: str, *, as_of: date | None = None
) -> NvdaSalePace:
    """The canonical NVDA sale-flow pace (see :class:`NvdaSalePace`).

    Glide arithmetic: with ``w(t)`` the glide's NVDA weight at date ``t``
    (clamped at the ends) and ``S0`` the shares held at plan start, the
    glide's implied share count at ``t`` is ``S0 * w(t) / w(start)``
    (weight and share count move proportionally on a same-book,
    same-price basis — the same assumption the glide's linear weight
    path already makes). ``S0`` is reconstructed as held-now (latest
    snapshot) + sold-since-plan-start; the ACTUAL Jan-1 holdings are
    held-now + sold-this-calendar-year. The tax-year quota is
    ``held(Jan 1) - implied(Dec 31)``; the sold-by-now expectation is
    ``held(Jan 1) - implied(today)``.
    """
    today = _today(as_of)
    sold_calendar = compute_nvda_shares_sold_ytd(session, user_id, as_of=today)
    # Without a plan window the pace window degrades to the calendar year:
    # sold stays populated (the ConcentrationAnalyst still reads the sales
    # history) even when no target exists.
    no_plan_pace = NvdaSalePace(
        sold_shares=sold_calendar, sold_calendar_ytd=sold_calendar,
        tax_year=today.year, sold_since_plan_start=sold_calendar,
    )

    try:
        from argosy.state.queries import get_current_plan, get_pending_draft

        pv = get_pending_draft(session, user_id) or get_current_plan(session, user_id)
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "nvda_sales_history.plan_lookup_failed",
            user_id=user_id, error=str(exc),
        )
        return no_plan_pace

    glide = _nvda_glide_weights(pv)
    if glide:
        plan_start, w_start = glide[0]
        w_end = glide[-1][1]
        if w_start > 0 and w_end < w_start:
            sold_since_start = (
                compute_nvda_shares_sold_ytd(
                    session, user_id, as_of=today, since=plan_start,
                )
                if today >= plan_start else 0
            )
            held_now = _nvda_shares_held_now(session, user_id)
            held_start = held_now + sold_since_start
            held_jan1 = held_now + sold_calendar

            def _implied(at: date) -> float:
                """Glide-implied share count at ``at`` (clamped)."""
                return held_start * _interp_weight(glide, at) / w_start

            year_end = date(today.year, 12, 31)
            # Tax-year quota: actual Jan-1 book minus the glide's implied
            # Dec-31 holdings. Never negative; the to-date expectation is
            # clamped inside [0, quota].
            annual = max(0, int(round(held_jan1 - _implied(year_end))))
            target = max(0, int(round(held_jan1 - _implied(today))))
            target = min(target, annual)

            # Next dated glide checkpoint strictly after today.
            next_wp: tuple[date, float] | None = next(
                ((d, w) for d, w in glide if d > today), None
            )
            wp_date = next_wp[0] if next_wp else None
            wp_weight = next_wp[1] if next_wp else None
            wp_shares = (
                max(0, int(round(held_now - _implied(wp_date))))
                if wp_date is not None else 0
            )

            pace = NvdaSalePace(
                target_shares=target,
                sold_shares=sold_calendar,
                annual_flow=annual,
                plan_start=plan_start,
                sold_calendar_ytd=sold_calendar,
                basis="glide",
                tax_year=today.year,
                sold_since_plan_start=sold_since_start,
                next_waypoint_date=wp_date,
                next_waypoint_weight_pct=wp_weight,
                shares_to_sell_by_waypoint=wp_shares,
            )
            log.info(
                "nvda_sales_history.sale_pace_glide",
                user_id=user_id, plan_start=plan_start.isoformat(),
                w_start=round(w_start, 4),
                held_start=round(held_start, 1), target=target,
                sold_calendar=sold_calendar, annual=annual,
                tax_year=today.year,
                next_waypoint=(wp_date.isoformat() if wp_date else None),
                shares_to_sell_by_waypoint=wp_shares,
            )
            return pace

    # Legacy fallback: medium-horizon annual share target, calendar proration.
    annual = _annual_nvda_target_from_plan(pv)
    if annual <= 0:
        log.info("nvda_sales_history.no_annual_nvda_target", user_id=user_id)
        return no_plan_pace
    days_elapsed = (today - _start_of_year(today)).days + 1  # inclusive of today
    target = int(round(annual * (days_elapsed / 365.0)))
    log.info(
        "nvda_sales_history.sale_pace_horizon_fallback",
        user_id=user_id, annual=annual,
        days_elapsed=days_elapsed, target=target,
    )
    return NvdaSalePace(
        target_shares=target,
        sold_shares=sold_calendar,
        annual_flow=annual,
        plan_start=_start_of_year(today),
        sold_calendar_ytd=sold_calendar,
        basis="horizon",
        tax_year=today.year,
        sold_since_plan_start=sold_calendar,
    )


def compute_nvda_target_shares_ytd(
    session: Session, user_id: str, *, as_of: date | None = None
) -> int:
    """Back-compat wrapper: the canonical pace's pro-rated target-shares.

    Plan-relative when the plan carries a glide doc ("ytd" then means
    PLAN-year-to-date); calendar-YTD only on the legacy horizon fallback.
    Returns 0 when no plan / no NVDA flow target is found — the home
    widget treats 0 as "no plan target yet" and renders a neutral badge.
    """
    return compute_nvda_sale_pace(session, user_id, as_of=as_of).target_shares


__all__ = [
    "NvdaSalePace",
    "compute_nvda_sale_pace",
    "compute_nvda_shares_sold_ytd",
    "compute_nvda_target_shares_ytd",
]
