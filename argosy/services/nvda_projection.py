"""Canonical NVDA deconcentration projection — the SINGLE source the /plan
NVDA-trajectory chart and the allocation glidepath both bind to.

The recurring failure mode this kills: every NVDA surface used to re-derive its
own view from a divergent source (``identity_yaml``, draft-only ceilings, a
snapshot category on the wrong basis), so the charts disagreed and nothing
failed when they did. Here there is exactly one selldown, expressed as a single
normalised path::

    norm(t) = shares(t) / today_shares          (1.0 today -> cap/current target)

Every surface is then just ``base * norm(t)`` in its own denominator:

    * share count        = today_shares      * norm(t)   (trajectory chart)
    * tradeable weight   = current_tradeable * norm(t)   (concentration view, 64.86% -> 13%)
    * full-book weight   = fullbook_current  * norm(t)   (canonical NVDA band; mirrors the tradeable weight)

Because the three are the same path in three denominators, the cross-surface
consistency guardrail is mechanically true: their normalised paths are
identical and all land at ``cap / current`` of today.

Money-math basis (Ariel-confirmed): E1 = proceeds redeploy *in-book* (the
tradeable book stays ~constant, so NVDA is a shrinking slice of a fixed pie);
E2 = the share target line is *flat-price*. At flat price the NVDA unit price
cancels, so::

    target_shares = floor(cap_pct / current_tradeable_pct * today_shares)

The glide *duration* is set by the plan's annual sale flow (never a magic
horizon): ``years = (today_shares - target_shares) / annual_reduction``.
"""
from __future__ import annotations

import calendar
import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class NvdaProjectionPoint:
    point_date: date
    norm: float                  # shares(t) / today_shares; 1.0 -> cap/current
    shares: int                  # flat-price implied NVDA share count
    tradeable_weight_pct: float  # NVDA % of the tradeable sub-book (0-100)
    fullbook_weight_pct: float   # NVDA % of the full liquid book (0-100)


@dataclass(frozen=True)
class NvdaProjection:
    today: date
    target_date: date
    today_shares: int
    target_shares: int
    target_norm: float
    current_tradeable_pct: float
    cap_pct: float
    fullbook_current_pct: float
    fullbook_target_pct: float
    annual_reduction: int
    points: list[NvdaProjectionPoint]

    # -- the one selldown, sampled at any date (linear today->target, then flat) --

    def norm_at(self, d: date) -> float:
        """norm(t) = shares(t)/today_shares; 1.0 today -> target_norm at target."""
        span = max((self.target_date - self.today).days, 1)
        frac = min(max((d - self.today).days / span, 0.0), 1.0)
        return 1.0 - frac * (1.0 - self.target_norm)

    def shares_at(self, d: date) -> int:
        return round(self.norm_at(d) * self.today_shares)

    def tradeable_weight_at(self, d: date) -> float:
        return self.current_tradeable_pct * self.norm_at(d)

    def fullbook_weight_at(self, d: date) -> float:
        return self.fullbook_current_pct * self.norm_at(d)


def _add_months(d: date, months: int) -> date:
    """Add whole months, clamping the day to the target month's length."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def build_nvda_projection(
    *,
    today: date,
    current_tradeable_pct: float,
    cap_pct: float,
    today_shares: int,
    fullbook_current_pct: float,
    annual_reduction: int,
    horizon_end: date | None = None,
) -> NvdaProjection:
    """Build the canonical NVDA selldown path. Pure: no DB, no clock.

    ``current_tradeable_pct`` / ``cap_pct`` / ``fullbook_current_pct`` are
    percentages on a 0-100 scale. ``annual_reduction`` is the planned net
    share reduction per year (the plan's deconcentration cadence).
    """
    if current_tradeable_pct <= 0:
        raise ValueError("current_tradeable_pct must be > 0")
    if annual_reduction <= 0:
        raise ValueError("annual_reduction must be > 0")

    target_norm = cap_pct / current_tradeable_pct
    target_shares = int(math.floor(target_norm * today_shares))
    fullbook_target_pct = fullbook_current_pct * target_norm

    years_to_target = (today_shares - target_shares) / float(annual_reduction)
    target_date = today + timedelta(days=round(years_to_target * 365.0))

    end = horizon_end if (horizon_end is not None and horizon_end > target_date) else target_date
    span_days = max((target_date - today).days, 1)

    def _point(d: date) -> NvdaProjectionPoint:
        frac = (d - today).days / span_days
        frac = min(max(frac, 0.0), 1.0)
        norm = 1.0 - frac * (1.0 - target_norm)
        return NvdaProjectionPoint(
            point_date=d,
            norm=norm,
            shares=round(norm * today_shares),
            tradeable_weight_pct=current_tradeable_pct * norm,
            fullbook_weight_pct=fullbook_current_pct * norm,
        )

    # Monthly grid from today, plus the two key dates (target + horizon end).
    grid: list[date] = []
    i = 0
    while True:
        d = _add_months(today, i)
        if d >= end:
            break
        grid.append(d)
        i += 1
    grid.extend([target_date, end])
    seen: set[date] = set()
    dates: list[date] = []
    for d in sorted(grid):
        if today <= d <= end and d not in seen:
            seen.add(d)
            dates.append(d)

    return NvdaProjection(
        today=today,
        target_date=target_date,
        today_shares=today_shares,
        target_shares=target_shares,
        target_norm=target_norm,
        current_tradeable_pct=current_tradeable_pct,
        cap_pct=cap_pct,
        fullbook_current_pct=fullbook_current_pct,
        fullbook_target_pct=fullbook_target_pct,
        annual_reduction=annual_reduction,
        points=[_point(d) for d in dates],
    )


# ---------------------------------------------------------------------------
# DB-aware wrapper — the single place every NVDA surface sources from.
# ---------------------------------------------------------------------------


def _nvda_shares_from_snapshot(snap) -> int | None:
    """Pull today's NVDA share count out of a snapshot's ``positions_json``."""
    if snap is None or not getattr(snap, "positions_json", None):
        return None
    try:
        positions = json.loads(snap.positions_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(positions, list):
        return None
    for p in positions:
        if not isinstance(p, dict):
            continue
        if (p.get("symbol") or "").upper() != "NVDA":
            continue
        sh = p.get("shares")
        if sh is None:
            sh = p.get("quantity")
        try:
            return int(round(float(sh)))
        except (TypeError, ValueError):
            return None
    return None


def compute_nvda_projection(
    db: "Session",
    user_id: str,
    today: date,
    *,
    horizon_end: date | None = None,
) -> NvdaProjection | None:
    """Resolve the canonical NVDA projection from the current plan + snapshot.

    Returns ``None`` (so surfaces degrade to "no data") when the current plan,
    the concentration cap/current weights, today's share count, the full-book
    anchor, or the planned annual flow is missing — never a guess.

    Sources (all canonical, none re-derived):
      * ``current_tradeable_pct`` / ``cap_pct`` ← the concentration analyst via
        ``resolve_plan_numbers`` (default ``include_canonical_ages=False`` — the
        concentration keys never enter the dual-track re-entrant hop).
      * ``today_shares`` ← latest snapshot ``positions_json`` NVDA row.
      * ``fullbook_current_pct`` ← the canonical NVDA weight (= the concentration
        report's ``current_tradeable_pct``); NOT the snapshot "Individual Stocks"
        row, which is the OTHER singles, not NVDA (codex-confirmed).
      * ``annual_reduction`` ← the plan's NVDA sale cadence.
    """
    from argosy.services.allocation_glidepath import (
        _latest_portfolio_snapshot,
    )
    from argosy.services.nvda_sales_history import _annual_nvda_target_from_plan
    from argosy.services.plan_numeric_resolver import resolve_plan_numbers
    from argosy.state.queries import get_current_plan

    pv = get_current_plan(db, user_id)
    if pv is None or getattr(pv, "decision_run_id", None) is None:
        return None

    nums = resolve_plan_numbers(db, user_id=user_id, decision_run_id=pv.decision_run_id)
    cap_rv = nums.get("concentration.nvda_cap_pct")
    cur_rv = nums.get("concentration.nvda_current_pct")
    if cap_rv.status != "resolved" or cur_rv.status != "resolved":
        return None
    if cap_rv.value is None or cur_rv.value is None or cur_rv.value <= 0:
        return None
    cap_pct = float(cap_rv.value) * 100.0
    current_tradeable_pct = float(cur_rv.value) * 100.0

    # Bind the cap to the canonical TargetAllocationDoc — the SAME single source
    # the glidepath + portfolio surfaces use — so the trajectory reconciles with
    # them by construction. Without this the projection reads the concentration
    # analyst's tail-loss cap (~7%) via the resolver (called here with
    # include_canonical_ages=False, so the canonical override doesn't apply),
    # while the other surfaces show the user-settled 13% — a surface split.
    try:
        import json as _json

        if getattr(pv, "target_allocation_json", None):
            _doc_cap = _json.loads(pv.target_allocation_json).get("nvda_cap_pct")
            if _doc_cap is not None:
                cap_pct = float(_doc_cap)  # doc carries percent-points
    except Exception:  # noqa: BLE001 — fall back to the resolver cap
        pass

    snap = _latest_portfolio_snapshot(db, user_id)
    today_shares = _nvda_shares_from_snapshot(snap)
    if today_shares is None or today_shares <= 0:
        return None

    # The full-book band uses the SAME canonical NVDA weight as the tradeable
    # band: ``current_tradeable_pct`` (the concentration report's NVDA share =
    # the doc's NVDA today weight). The prior code sourced this from the snapshot
    # "Individual Stocks" category, which is the OTHER singles (GOOG/AMZN/.../RKT),
    # NOT NVDA — a codex-confirmed root-confusion bug (it read 18.21% instead of
    # ~64.86%). There is ONE canonical NVDA weight; the glidepath/portfolio bands
    # read it from the TargetAllocationDoc, and this projection mirrors it.
    fullbook_current_pct = current_tradeable_pct

    annual = _annual_nvda_target_from_plan(pv)
    if annual <= 0:
        return None

    return build_nvda_projection(
        today=today,
        current_tradeable_pct=current_tradeable_pct,
        cap_pct=cap_pct,
        today_shares=int(today_shares),
        fullbook_current_pct=float(fullbook_current_pct),
        annual_reduction=int(annual),
        horizon_end=horizon_end,
    )
