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

Also exposes the ONE canonical NVDA sale-flow derivation,
``compute_nvda_sale_pace``: the target flow comes from the plan's
``TargetAllocationDoc`` GLIDE (the canonical, structured, cap-derived
source — NVDA weight waypoints today → end-state), converted to shares
via the held-at-plan-start share count, and pro-rated PLAN-RELATIVE
(from the glide's start date, not Jan 1). ``compute_nvda_target_shares_ytd``
is a thin back-compat wrapper over it. The legacy medium-horizon
``shares``-unit target + calendar-YTD proration survives only as the
fallback when a plan carries no glide doc.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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


def _sum_monthly_sales(
    sales: list[Any], *, anchor_year: int, as_of: date, since: date | None = None
) -> int:
    """Sum month-granular sale rows for the ``[since or Jan 1 .. as_of]`` window.

    Shared by the snapshot- and TSV-sourced branches. Rows may be dicts
    (snapshot ``nvda_sales_json``) or objects (parsed TSV). Dedups on
    ``(month, shares)`` — the source occasionally repeats a row (observed
    in run #25's snapshot and again in dev snapshot 12's double Apr row).

    ``since`` filters at MONTH granularity (the source has no day): a row in
    ``since``'s own month counts as inside the window — the approximation is
    at most one month wide and only on the window's first month.
    """
    def _get(s: Any, key: str) -> Any:
        if isinstance(s, dict):
            return s.get(key)
        return getattr(s, key, None)

    seen: set[tuple[str, int]] = set()
    total = 0
    for s in sales:
        month = _get(s, "month")
        shares = _get(s, "shares")
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
        key = (str(month).strip().lower(), shares_int)
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
    plan-relative pace (``compute_nvda_sale_pace``) passes the plan's
    start date instead.

    Source priority:
      1. ``fills`` table when it has at least one NVDA row for ``user_id``.
      2. The latest portfolio snapshot's ``nvda_sales_json`` block —
         internal state (the TSV is OUTPUT-only). Month-granular.
      3. Fallback to the latest Family Finances Status TSV's
         ``nvda_sales`` block. (Month-granular; current-year only.)

    Returns 0 when no source produces data (the caller surfaces that
    as "no fills found" rather than crashing synthesis).
    """
    today = _today(as_of)
    window_start = since or _start_of_year(today)

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
    """The ONE canonical NVDA sale-flow pace.

    ``basis="glide"``: target flow derives from the plan's
    ``TargetAllocationDoc`` glide (NVDA weight waypoints), converted to
    shares via the held-at-plan-start count, pro-rated from the PLAN's
    start date (the glide's first waypoint) — being "behind" thousands
    of shares one day into the plan year is a calendar artifact, not a
    pace signal. ``sold_shares`` counts sales since plan start;
    ``sold_calendar_ytd`` keeps the old calendar figure as context.

    ``basis="horizon"``: legacy fallback (no glide doc on the plan) —
    the medium-horizon ``shares``-unit annual target pro-rated
    calendar-YTD, ``sold_shares`` = calendar YTD.

    ``basis="none"``: no plan / no NVDA flow target anywhere.
    """

    target_shares: int = 0        # shares the plan expects sold by as_of
    sold_shares: int = 0          # shares actually sold in the pace window
    annual_flow: int = 0          # full plan-year target flow
    plan_start: date | None = None
    sold_calendar_ytd: int = 0
    basis: str = "none"

    @property
    def delta_shares(self) -> int:
        return self.sold_shares - self.target_shares

    @property
    def tolerance_shares(self) -> int:
        """Pace band: ±5% of the ANNUAL flow (≈ ±18 days of pace), min 10.

        Banding against the pro-rated target instead would shrink the band
        to nothing at plan start and flag day-1 "behind by 27 shares" noise.
        """
        return max(10, int(round(0.05 * self.annual_flow)))

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
    and ``S0`` the shares held at plan start, the glide's implied share
    count at ``t`` is ``S0 * w(t) / w(start)`` (weight and share count
    move proportionally on a same-book, same-price basis — the same
    assumption the glide's linear weight path already makes), so the
    target flow by ``t`` is ``S0 * (1 - w(t)/w(start))``. ``S0`` is
    reconstructed as held-now (latest snapshot) + sold-since-plan-start.
    """
    today = _today(as_of)
    sold_calendar = compute_nvda_shares_sold_ytd(session, user_id, as_of=today)
    # Without a plan window the pace window degrades to the calendar year:
    # sold stays populated (the ConcentrationAnalyst still reads the sales
    # history) even when no target exists.
    no_plan_pace = NvdaSalePace(
        sold_shares=sold_calendar, sold_calendar_ytd=sold_calendar,
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
            held_start = _nvda_shares_held_now(session, user_id) + sold_since_start
            w_now = _interp_weight(glide, today)
            target = int(round(held_start * (1.0 - w_now / w_start)))
            annual = int(round(held_start * (1.0 - w_end / w_start)))
            pace = NvdaSalePace(
                target_shares=target,
                sold_shares=sold_since_start,
                annual_flow=annual,
                plan_start=plan_start,
                sold_calendar_ytd=sold_calendar,
                basis="glide",
            )
            log.info(
                "nvda_sales_history.sale_pace_glide",
                user_id=user_id, plan_start=plan_start.isoformat(),
                w_start=round(w_start, 4), w_now=round(w_now, 4),
                held_start=round(held_start, 1), target=target,
                sold=sold_since_start, annual=annual,
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
