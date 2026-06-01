"""Allocation glidepath service (Wave 8 Piece B1).

Computes a month-by-month projected portfolio composition by linearly
interpolating between today's snapshot and each in-scope target's
``revisit_after`` date.

Inclusion rule (v1, per spec): only ``pct_of_portfolio`` and
``pct_of_liquid`` targets enter the glidepath. Other-unit targets
(``usd``, ``nis``, ``shares``, ``months``, …) are surfaced via
``excluded_targets`` so the UI can route them into the actions
timeline (Piece F) instead.

Direction-reversal guardrail (v1, locked per zigzag round 3): if an
intermediate waypoint reverses direction relative to ``today_value``
and the eventual endpoint (e.g., current NVDA 64.9% → medium 70% →
long 15%), the intermediate is **always collapsed**. No opt-out, no
synthesizer schema extension — YAGNI. Each collapsed waypoint is
loudly logged in ``collapsed_waypoints`` so audit + UI can surface
the decision.

Asset-class identity: the synthesizer's ``SynthTarget.label`` is the
asset-class key (case-insensitive, whitespace-trimmed). When the same
label appears in multiple horizons, all targets become waypoints
ordered by ``revisit_after``. Today's value comes from
``portfolio_categories`` by case-insensitive label match, or 0.0
when the target's label has no counterpart in the snapshot (i.e.
the plan is introducing a new asset class).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.agents.plan_synthesizer_types import HorizonSection, SynthTarget
from argosy.state.models import PlanVersion, PortfolioSnapshotRow
from argosy.state.queries import get_current_plan

logger = logging.getLogger(__name__)


# Unit literals that count as portfolio-percentage targets for the
# glidepath. Per spec: only ``pct_of_portfolio`` + ``pct_of_liquid``.
# ``pct_of_net_worth`` is excluded — that lens includes pension +
# real-estate assets which aren't part of the "portfolio mix".
_PCT_UNITS = frozenset({"pct_of_portfolio", "pct_of_liquid"})


# Wave 8 v2 polish — bridge alias map for synthesizer label → snapshot
# category matching. The synthesizer emits descriptive prose labels
# (e.g. "info-tech sector cap on managed portfolio (excludes nvda
# rsu)") that don't match the snapshot's short category names ("Growth",
# "Core Equity", "Individual Stocks", etc.). Until the synthesizer's
# output schema is extended to carry an explicit ``asset_class_key``
# (v2 follow-on per codex round 2), this keyword table catches the
# common cases so the glidepath at least anchors on a real today
# value when the label mentions a recognizable asset class.
#
# Keys are lowercase substrings to look for in the synthesizer label.
# Values are ordered tuples of candidate snapshot categories — first
# match wins. Snapshot category names are matched case-insensitively
# via ``_normalize_label``.
_LABEL_KEYWORD_TO_SNAPSHOT_CATEGORY: dict[str, tuple[str, ...]] = {
    "nvda": ("Individual Stocks",),
    "nvidia": ("Individual Stocks",),
    "sgov": ("Cash",),
    "treasury": ("Cash",),
    "t-bill": ("Cash",),
    "cash": ("Cash",),
    "liquid": ("Cash",),
    "core equity": ("Core Equity",),
    "core-equity": ("Core Equity",),
    "growth": ("Growth",),
    "dividend": ("Dividend",),
    "international": ("International",),
    "defensive": ("Defensive",),
    "alternative": ("Alternative",),
    "real estate": ("REIT", "Real estate"),
    "reit": ("REIT",),
    "info-tech": ("Growth",),  # info-tech maps to the growth bucket
    "tech sector": ("Growth",),
    "us-equity": ("Core Equity", "Growth"),
    "us equity": ("Core Equity", "Growth"),
    "fixed income": ("Defensive",),
    "bond": ("Defensive",),
    "individual stock": ("Individual Stocks",),
}


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GlidepathPoint:
    """Projected composition at a single month tick along the glidepath."""

    months_out: int
    point_date: date  # first-of-month
    composition_pct_by_class: dict[str, float]  # asset_class (lowercased) → 0-100


@dataclass(frozen=True)
class CollapsedWaypoint:
    """An intermediate waypoint that was skipped because it reversed
    direction relative to today + the eventual endpoint."""

    asset_class: str
    waypoint_date: date
    target_pct: float
    source_horizon: str  # "long" | "medium" | "short"
    reason: str


@dataclass(frozen=True)
class ExcludedTarget:
    """A target that didn't qualify for the glidepath (non-pct unit)."""

    target_label: str
    target_unit: str
    target_value: float
    target_date: date
    reason: str


@dataclass(frozen=True)
class AssetClassAnchorStatus:
    """Per-class diagnostic for the chart. Was today's value pulled
    from a real snapshot category match (``matched=True``), an alias-
    map keyword match (``matched=True`` + ``alias_source``), or did
    the matcher fall back to 0 (``matched=False``)?"""

    asset_class: str  # the synthesizer label, lowercased
    matched: bool
    today_value: float
    # When matched via the alias keyword table, this is the snapshot
    # category name it routed to (so the chart can show "→ Growth").
    alias_source: str | None = None


@dataclass(frozen=True)
class AllocationGlidepath:
    """Top-level result returned to the route."""

    points: list[GlidepathPoint]
    collapsed_waypoints: list[CollapsedWaypoint]
    excluded_targets: list[ExcludedTarget]
    asset_classes: list[str] = field(default_factory=list)
    anchor_status: list[AssetClassAnchorStatus] = field(default_factory=list)
    today: date | None = None
    end_date: date | None = None


# ---------------------------------------------------------------------------
# Pure-function math layer
# ---------------------------------------------------------------------------


def filter_targets_by_pct_unit(
    targets: list[SynthTarget],
) -> tuple[list[SynthTarget], list[ExcludedTarget]]:
    """Split a target list into pct-unit-eligible vs excluded.

    Pure: no DB, no clock. Preserves the input ordering of the
    eligible list so downstream grouping behaviour stays deterministic.
    """
    eligible: list[SynthTarget] = []
    excluded: list[ExcludedTarget] = []
    for t in targets:
        if t.unit in _PCT_UNITS:
            eligible.append(t)
            continue
        excluded.append(
            ExcludedTarget(
                target_label=t.label,
                target_unit=t.unit,
                target_value=t.value,
                target_date=t.revisit_after,
                reason=(
                    f"non-pct unit ({t.unit}) — surfaced in the actions "
                    f"timeline instead of the glidepath"
                ),
            )
        )
    return eligible, excluded


def _normalize_label(label: str) -> str:
    return label.strip().lower()


def _resolve_today_value(
    label_lower: str,
    portfolio_categories: dict[str, float],
) -> tuple[float, bool, str | None]:
    """Find today's value for an asset-class label.

    Returns ``(value, matched, alias_source)``.

    Match strategy:
      1. Exact-match the lowercase label against snapshot categories.
      2. Otherwise walk the alias keyword table and try each
         keyword-substring against the label; for the first hit, try
         each candidate snapshot category in order.
      3. Fall back to 0.0 + matched=False if nothing hits.
    """
    if label_lower in portfolio_categories:
        return portfolio_categories[label_lower], True, None
    for kw, candidates in _LABEL_KEYWORD_TO_SNAPSHOT_CATEGORY.items():
        if kw not in label_lower:
            continue
        for cat in candidates:
            cat_lower = _normalize_label(cat)
            if cat_lower in portfolio_categories:
                return portfolio_categories[cat_lower], True, cat
    return 0.0, False, None


def group_targets_by_label(
    targets: list[SynthTarget],
) -> dict[str, list[SynthTarget]]:
    """Group eligible targets by case-insensitive label.

    Preserves first-encounter ordering of label keys (insertion-order
    dict) so the UI's chart band order matches the spec table order.
    Within each group, targets are sorted by ``revisit_after``
    ascending so the waypoint sequence has a stable date order.
    """
    groups: dict[str, list[SynthTarget]] = {}
    for t in targets:
        key = _normalize_label(t.label)
        groups.setdefault(key, []).append(t)
    for key in groups:
        groups[key].sort(key=lambda t: t.revisit_after)
    return groups


def collapse_direction_reversals(
    *,
    today_value: float,
    waypoints: list[tuple[date, float, str]],
) -> tuple[list[tuple[date, float, str]], list[CollapsedWaypoint]]:
    """Strip intermediate waypoints that reverse direction relative to
    ``today_value`` and the eventual (last) waypoint.

    ``waypoints`` is sorted by date ascending. The last entry is the
    "eventual" endpoint. Any earlier waypoint whose value sits on the
    opposite side of ``today_value`` from the eventual is collapsed.

    When ``today_value`` equals the eventual exactly (macro direction
    is 0), nothing is collapsed — there's no direction to reverse.

    Returns ``(kept_waypoints, collapsed)``. The caller is responsible
    for logging warnings if ``collapsed`` is non-empty.
    """
    if not waypoints:
        return [], []
    if len(waypoints) == 1:
        # Single waypoint is the eventual; no intermediates to check.
        return list(waypoints), []
    eventual_value = waypoints[-1][1]
    macro_delta = eventual_value - today_value
    if macro_delta == 0:
        return list(waypoints), []
    macro_sign = 1 if macro_delta > 0 else -1

    kept: list[tuple[date, float, str]] = []
    collapsed: list[CollapsedWaypoint] = []
    # Iterate over all but the last waypoint (= the eventual stays).
    for w in waypoints[:-1]:
        w_date, w_value, w_horizon = w
        delta_from_today = w_value - today_value
        sign = 0 if delta_from_today == 0 else (1 if delta_from_today > 0 else -1)
        if sign != 0 and sign != macro_sign:
            collapsed.append(
                CollapsedWaypoint(
                    asset_class="",  # filled in by caller (has the label)
                    waypoint_date=w_date,
                    target_pct=w_value,
                    source_horizon=w_horizon,
                    reason=(
                        f"direction-reversal: current={today_value:.2f}, "
                        f"intermediate={w_value:.2f}, eventual={eventual_value:.2f}"
                    ),
                )
            )
            continue
        kept.append(w)
    kept.append(waypoints[-1])
    return kept, collapsed


def _months_between(start: date, end: date) -> int:
    """Number of whole month transitions between two first-of-month
    dates. ``end`` >= ``start``. Returns 0 if ``start == end``."""
    if end < start:
        return 0
    return (end.year - start.year) * 12 + (end.month - start.month)


def _first_of_month(d: date) -> date:
    return date(d.year, d.month, 1)


def _add_months(start: date, months: int) -> date:
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def _months_since_start(target: date, start: date) -> float:
    """Fractional months between two dates (used for interpolation
    fraction inside the bracketing pair). Handles month-of-year + day
    proportionally so a waypoint at 2026-12-15 sits between the
    2026-12 and 2027-01 chart ticks at ~50%."""
    months = (target.year - start.year) * 12 + (target.month - start.month)
    # Add fraction of month from the day component (rough — same day
    # count regardless of month length, which is fine at glidepath
    # resolution).
    months += (target.day - 1) / 30.0
    return float(months)


def interpolate_glidepath(
    *,
    per_class_waypoints: dict[str, list[tuple[date, float]]],
    today: date,
    end_date: date,
) -> list[GlidepathPoint]:
    """Produce one ``GlidepathPoint`` per month tick from ``today``
    (= month 0) to ``end_date`` (= month N) inclusive.

    For each class, the value at tick t is linearly interpolated
    between the two bracketing waypoints. If t is past the last
    waypoint, the value holds flat at the last waypoint. If t is
    before the first waypoint (shouldn't happen because today is
    waypoint 0 by convention), the value holds flat at the first.

    The output is sorted by ``months_out`` ascending.
    """
    today_first = _first_of_month(today)
    end_first = _first_of_month(end_date)
    n_months = _months_between(today_first, end_first)
    if n_months < 0:
        n_months = 0

    points: list[GlidepathPoint] = []
    for i in range(n_months + 1):
        tick_date = _add_months(today_first, i)
        tick_offset_months = float(i)
        comp: dict[str, float] = {}
        for cls, waypoints in per_class_waypoints.items():
            if not waypoints:
                continue
            value = _interpolate_one_class(
                waypoints=waypoints,
                tick_offset_months=tick_offset_months,
                today=today_first,
            )
            comp[cls] = value
        points.append(
            GlidepathPoint(
                months_out=i,
                point_date=tick_date,
                composition_pct_by_class=comp,
            )
        )
    return points


def _interpolate_one_class(
    *,
    waypoints: list[tuple[date, float]],
    tick_offset_months: float,
    today: date,
) -> float:
    """Linear-interpolate one asset class at the given tick offset."""
    # Project each waypoint into month-offsets from today.
    series = [
        (_months_since_start(w_date, today), w_value)
        for (w_date, w_value) in waypoints
    ]
    # Flat-extend before first / after last.
    if tick_offset_months <= series[0][0]:
        return series[0][1]
    if tick_offset_months >= series[-1][0]:
        return series[-1][1]
    # Find the bracketing pair.
    for i in range(len(series) - 1):
        a_t, a_v = series[i]
        b_t, b_v = series[i + 1]
        if a_t <= tick_offset_months <= b_t:
            if b_t == a_t:
                return b_v
            frac = (tick_offset_months - a_t) / (b_t - a_t)
            return a_v + (b_v - a_v) * frac
    # Defensive fallback (should never reach here given the flat-extend
    # guards above).
    return series[-1][1]


def _normalize_pct_value(v: float, peers: list[float]) -> float:
    """Coerce a single percentage value onto the 0-100 scale.

    Synthesizers in the wild emit ``pct_of_portfolio`` as either a
    whole-percentage (``35.0`` meaning 35%) OR a unit fraction
    (``0.35`` meaning 35%). We normalize per asset class: if EVERY
    value in the class (today + all waypoints) is ≤ 1.0, the whole
    series is treated as fraction-of-1 and multiplied by 100. Otherwise
    leave alone. Mixed scales within one class would be a synthesizer
    bug and aren't auto-detected here.
    """
    if not peers:
        return v
    max_peer = max(abs(p) for p in peers)
    if max_peer <= 1.0:
        return v * 100.0
    return v


def _matched_categories(
    eligible_labels: list[str], portfolio_categories: dict[str, float]
) -> set[str]:
    """Return the set of portfolio_categories keys (lowercased) that
    a target label has bound to — either via exact match or alias
    keyword routing. Used to identify the "untargeted" snapshot
    categories that should still be flat-lined on the chart so the
    user sees their whole portfolio mix, not just the synth-targeted
    slices (codex deep-audit finding #5)."""
    matched: set[str] = set()
    for label_lower in eligible_labels:
        if label_lower in portfolio_categories:
            matched.add(label_lower)
            continue
        for kw, candidates in _LABEL_KEYWORD_TO_SNAPSHOT_CATEGORY.items():
            if kw not in label_lower:
                continue
            for cat in candidates:
                cat_lower = _normalize_label(cat)
                if cat_lower in portfolio_categories:
                    matched.add(cat_lower)
                    break
            else:
                continue
            break
    return matched


def build_glidepath(
    *,
    portfolio_categories: dict[str, float],
    targets: list[SynthTarget],
    today: date,
) -> AllocationGlidepath:
    """Full pipeline: filter pct-unit, group by label, collapse
    direction reversals, anchor with today's snapshot, interpolate.

    Pure: no DB, no clock. Accepts ``today`` as input so tests can
    pin a specific reference date.

    ``portfolio_categories`` keys must already be lowercased / trimmed
    (callers from the DB orchestrator below pass through the same
    ``_normalize_label`` step). Values are 0-100.

    Synthesizer pct-scale: ``SynthTarget.value`` may arrive as either
    whole-percentage (``35.0``) or fraction-of-1 (``0.35``). We
    auto-detect per asset class and normalize to 0-100 so the chart
    always sees consistent units.

    Wave 8 v2 polish (codex deep-audit #5) — UNION semantics: classes
    appear on the chart for both (a) every target label and (b) every
    snapshot category not already bound to a target. Untargeted
    snapshot bands flat-line at today's value across the horizon and
    surface as ``anchor_status.matched=True + alias_source="snapshot"``
    so the UI can label them as "unconstrained" rather than missing.
    """
    today_first = _first_of_month(today)
    eligible, excluded = filter_targets_by_pct_unit(targets)
    if not eligible:
        return AllocationGlidepath(
            points=[],
            collapsed_waypoints=[],
            excluded_targets=excluded,
            asset_classes=[],
            anchor_status=[],
            today=today_first,
            end_date=None,
        )
    groups = group_targets_by_label(eligible)

    per_class_waypoints: dict[str, list[tuple[date, float]]] = {}
    collapsed_total: list[CollapsedWaypoint] = []
    max_revisit: date | None = None

    anchor_status_list: list[AssetClassAnchorStatus] = []
    for label_lower, group in groups.items():
        raw_today_value, matched, alias_source = _resolve_today_value(
            label_lower, portfolio_categories
        )
        # Codex B1 finding #3 — surface the no-match case so the UI
        # can render a "starts from 0" caveat instead of silently
        # showing a misleading rising-from-zero curve.
        if not matched:
            logger.warning(
                "allocation_glidepath.no_snapshot_match asset_class=%s "
                "anchor=0.0 (target label not present in portfolio_snapshot, "
                "and no alias-keyword routed it to a snapshot category)",
                label_lower,
            )
        anchor_status_list.append(
            AssetClassAnchorStatus(
                asset_class=label_lower,
                matched=matched,
                today_value=raw_today_value,
                alias_source=alias_source,
            )
        )
        raw_waypoints: list[tuple[date, float, str]] = []
        for t in group:
            horizon = _horizon_from_target(t)
            raw_waypoints.append(
                (t.revisit_after, float(t.value), horizon)
            )
        # Per-class scale normalisation. Today's value comes from the
        # snapshot (already 0-100) but the synthesizer's targets may be
        # fraction-of-1; if every target waypoint is ≤ 1, scale them up.
        target_values = [v for _, v, _ in raw_waypoints]
        scaled_target_values = [
            _normalize_pct_value(v, target_values) for v in target_values
        ]
        raw_waypoints = [
            (raw_waypoints[i][0], scaled_target_values[i], raw_waypoints[i][2])
            for i in range(len(raw_waypoints))
        ]
        today_value = raw_today_value
        kept, collapsed = collapse_direction_reversals(
            today_value=today_value, waypoints=raw_waypoints
        )
        # Stamp asset_class onto the collapsed warnings + log them.
        for cw in collapsed:
            stamped = CollapsedWaypoint(
                asset_class=label_lower,
                waypoint_date=cw.waypoint_date,
                target_pct=cw.target_pct,
                source_horizon=cw.source_horizon,
                reason=cw.reason,
            )
            logger.warning(
                "allocation_glidepath.waypoint_collapsed asset_class=%s "
                "date=%s value=%.2f reason=%s",
                label_lower,
                cw.waypoint_date.isoformat(),
                cw.target_pct,
                cw.reason,
            )
            collapsed_total.append(stamped)
        # Anchor t=0 with today's value, then append kept waypoints.
        anchored: list[tuple[date, float]] = [(today_first, today_value)]
        for (w_date, w_value, _h) in kept:
            anchored.append((w_date, w_value))
            if max_revisit is None or w_date > max_revisit:
                max_revisit = w_date
        per_class_waypoints[label_lower] = anchored

    if max_revisit is None:
        # No eligible waypoints survived; degrade to empty (still
        # surface untargeted snapshot bands so the chart can render
        # the user's current allocation as flat lines).
        untargeted_only = _add_untargeted_snapshot_bands(
            per_class_waypoints={},
            anchor_status=anchor_status_list,
            portfolio_categories=portfolio_categories,
            today_first=today_first,
            horizon_end=today_first,
        )
        return AllocationGlidepath(
            points=[],
            collapsed_waypoints=collapsed_total,
            excluded_targets=excluded,
            asset_classes=list(untargeted_only.keys()),
            anchor_status=anchor_status_list,
            today=today_first,
            end_date=None,
        )

    # Wave 8 v2 polish — union the untargeted snapshot categories so
    # the chart shows the user's full current mix, not just the synth-
    # targeted slices. Untargeted bands flat-line at today's value;
    # the codex deep-audit flagged the missing-categories issue as the
    # root cause of the user's "the plan doesn't have more categories?"
    # complaint.
    per_class_waypoints = _add_untargeted_snapshot_bands(
        per_class_waypoints=per_class_waypoints,
        anchor_status=anchor_status_list,
        portfolio_categories=portfolio_categories,
        today_first=today_first,
        horizon_end=max_revisit,
    )

    points = interpolate_glidepath(
        per_class_waypoints=per_class_waypoints,
        today=today_first,
        end_date=max_revisit,
    )
    return AllocationGlidepath(
        points=points,
        collapsed_waypoints=collapsed_total,
        excluded_targets=excluded,
        asset_classes=list(per_class_waypoints.keys()),
        anchor_status=anchor_status_list,
        today=today_first,
        end_date=max_revisit,
    )


def _add_untargeted_snapshot_bands(
    *,
    per_class_waypoints: dict[str, list[tuple[date, float]]],
    anchor_status: list[AssetClassAnchorStatus],
    portfolio_categories: dict[str, float],
    today_first: date,
    horizon_end: date,
) -> dict[str, list[tuple[date, float]]]:
    """Mutate-and-return: extend ``per_class_waypoints`` with one
    flat-line band per untargeted snapshot category. Also append an
    AssetClassAnchorStatus row for each so the UI can label these as
    "unconstrained — held flat at today's value".
    """
    matched_cats = _matched_categories(
        list(per_class_waypoints.keys()), portfolio_categories
    )
    for cat_lower, pct in portfolio_categories.items():
        if cat_lower in matched_cats or cat_lower in per_class_waypoints:
            continue
        # Two waypoints: today and the horizon end, both at the
        # current snapshot value. Interpolation between them is the
        # constant function.
        per_class_waypoints[cat_lower] = [
            (today_first, pct),
            (horizon_end, pct),
        ]
        anchor_status.append(
            AssetClassAnchorStatus(
                asset_class=cat_lower,
                matched=True,
                today_value=pct,
                alias_source="snapshot (unconstrained — no plan target)",
            )
        )
    return per_class_waypoints


def _horizon_from_target(t: SynthTarget) -> str:
    """Best-effort horizon label from a SynthTarget.

    The synthesizer doesn't stamp horizon on the target itself; it's
    implied by which HorizonSection the target lives in. The DB
    orchestrator threads horizon through by reading the right
    ``horizon_*_json`` cell; for the pure-function tests where we
    construct SynthTarget directly without a HorizonSection wrapper,
    the horizon is unknown and we fall back to "unknown". Cosmetic
    only — used in the collapsed-waypoint warning line.
    """
    return getattr(t, "_glidepath_horizon", "unknown")


# ---------------------------------------------------------------------------
# DB-aware orchestrator
# ---------------------------------------------------------------------------


def _categories_from_snapshot(
    snap: PortfolioSnapshotRow | None,
) -> dict[str, float]:
    """Pull asset-class composition out of a portfolio_snapshot's
    ``allocations_json``. Returns ``{}`` when the row is missing,
    malformed, or has no allocation rows.

    Filters out summary rows the TSV ingest produces ("Grand Total",
    "Total", etc.) so they don't pollute the glidepath as fake
    asset-class bands."""
    if snap is None or not snap.allocations_json:
        return {}
    try:
        payload = json.loads(snap.allocations_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, list):
        return {}
    out: dict[str, float] = {}
    summary_labels = {"grand total", "total", "sum", "subtotal"}
    for row in payload:
        if not isinstance(row, dict):
            continue
        cat = row.get("category")
        pct = row.get("pct")
        if not isinstance(cat, str):
            continue
        cat_lower = _normalize_label(cat)
        if cat_lower in summary_labels:
            continue
        try:
            pct_f = float(pct) if pct is not None else 0.0
        except (TypeError, ValueError):
            continue
        # Drop near-zero allocations from the chart to reduce noise
        # (categories with 0% don't add information for a glidepath).
        if pct_f <= 0.01:
            continue
        out[cat_lower] = pct_f
    return out


def _targets_from_plan(pv: PlanVersion) -> list[SynthTarget]:
    """Collect SynthTargets from the current plan's three horizon
    JSON cells, stamping each with a ``_glidepath_horizon`` attribute
    so collapsed-waypoint warnings can name the source horizon."""
    out: list[SynthTarget] = []
    for field_name, horizon in (
        ("horizon_long_json", "long"),
        ("horizon_medium_json", "medium"),
        ("horizon_short_json", "short"),
    ):
        raw = getattr(pv, field_name, None)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        try:
            section = HorizonSection.model_validate(payload)
        except Exception:  # pydantic ValidationError or any other
            continue
        for t in section.targets:
            # Stamp the horizon onto the pydantic instance so the
            # collapsed-waypoint warning can include it. pydantic v2
            # allows arbitrary attribute assignment by default for
            # frozen=False models; SynthTarget is not frozen so this
            # works.
            try:
                object.__setattr__(t, "_glidepath_horizon", horizon)
            except (AttributeError, TypeError):
                pass
            out.append(t)
    return out


def _latest_portfolio_snapshot(
    db: Session, user_id: str
) -> PortfolioSnapshotRow | None:
    return (
        db.execute(
            select(PortfolioSnapshotRow)
            .where(PortfolioSnapshotRow.user_id == user_id)
            .order_by(desc(PortfolioSnapshotRow.imported_at))
            .limit(1)
        )
    ).scalar_one_or_none()


def compute_allocation_glidepath(
    db: Session,
    user_id: str,
    today: date,
) -> AllocationGlidepath | None:
    """Top-level entry. Returns the glidepath payload or None when no
    current plan exists for ``user_id``."""
    pv = get_current_plan(db, user_id)
    if pv is None:
        return None
    snap = _latest_portfolio_snapshot(db, user_id)
    portfolio_categories = _categories_from_snapshot(snap)
    targets = _targets_from_plan(pv)
    return build_glidepath(
        portfolio_categories=portfolio_categories,
        targets=targets,
        today=today,
    )
