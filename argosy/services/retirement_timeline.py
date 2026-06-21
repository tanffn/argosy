"""Holistic-timeline composer for the /retirement page (sprint commit #10).

Builds a single chronological payload combining:

  * Historical RSU vest events (from ``rsu_vest_events``).
  * Projected future RSU vests. Primary source is the historical-cadence
    heuristic over ``rsu_vest_events``; when that table is empty (the
    primary household carries its vests in the canonical
    ``identity_yaml.rsu_vest_schedule`` instead), the builder falls back
    to ``rsu_savings.project_quarterly_vests`` over the same
    active_grants + portal calendar the overview RSU chapter reads, so
    the markers reconcile with that chapter. Display-only.
  * Life events with a fixed target_date (from ``life_events``); when no
    dated rows exist, falls back to the canonical spending-phase curve
    (``phase_expenses.build_phase_expense_curve``) translated to dates.
  * Retire-ready-age zones for the three scenarios (bear / base / bull)
    via the canonical ``effective_retire_ready_age()`` clamp. The three
    scenarios legitimately collapse onto today when the household is
    already past FI under every regime (crossing clears at t=0); the
    builder emits the real per-scenario computation, never a hardcoded
    today-clamp.

The UI consumer (<HolisticTimelineCard>) renders these layered markers
on a single timeline without having to sort or de-duplicate -- this
module does both.

Spec: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
Section "Holistic Timeline card".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from sqlalchemy.orm import Session

from argosy.services.cashflow_projection import (
    EffectiveRetireReadyAge,
    _add_months,
    effective_retire_ready_age,
    extract_household_state,
)
from argosy.state.models import LifeEvent, RsuVestEvent

logger = __import__("logging").getLogger(__name__)

# Retention applied to the gross vest value for the display-only estimated
# net figure. Mirrors ``overview_assembler._RSU_CAPITAL_TRACK_RETENTION`` so
# the timeline tooltip reconciles with the overview RSU chapter to the shekel.
# (RSU vesting is ordinary income at vest; ~32% effective withholding.)
_RSU_CAPITAL_TRACK_RETENTION = 0.68


# v1 cadence heuristic -- same constant as the private projector in
# ``cashflow_projection``. NVDA RSU grants for Ariel run quarterly so 90d
# is the right tranche spacing. Annual / cliff grants will be over-
# projected here; a grant-aware follow-on lands in a later commit.
_QUARTERLY_VEST_DAYS = 90

# Hard cap on the number of projected future vests we emit. 12 ≈ 3 years
# of quarterly tranches -- past that the cadence heuristic stops being
# meaningfully accurate (grants expire, new ones land, FMV drifts).
MAX_FUTURE_VESTS = 12


@dataclass(frozen=True)
class VestMarker:
    """One vest marker -- historical OR projected.

    ``fmv_per_share_usd`` and ``estimated_gross_usd`` are None for
    projected vests when the source row has no FMV (shouldn't happen in
    practice, but the dataclass is defensive).
    """

    kind: Literal["past_vest", "future_vest"]
    date: date
    symbol: str
    grant_id: str
    shares: float
    fmv_per_share_usd: float | None
    estimated_gross_usd: float | None


@dataclass(frozen=True)
class LifeEventMarker:
    """One scheduled life event with a fixed target_date.

    Recurring life-event patterns (rows with target_date = NULL) are
    intentionally excluded -- those render as expense bands elsewhere,
    not as point-in-time timeline markers.
    """

    date: date
    category: str
    kind: str
    amount_usd: float | None
    description: str | None


@dataclass(frozen=True)
class RetireZone:
    """One retire-ready-age zone (bear / base / bull).

    ``expected_date`` is the calendar date that ``age_years`` resolves
    to using the household's current_age_years anchor + as_of. This
    lets the UI render the zone on the same date axis as vests and
    life events without re-doing the age->date math.
    """

    scenario: Literal["bear", "base", "bull"]
    age_years: float
    expected_date: date
    clamp_reason: str


@dataclass(frozen=True)
class HolisticTimeline:
    """Composite payload returned by ``build_holistic_timeline()``.

    Marker lists are pre-sorted by date so the UI can render top-to-
    bottom without re-sorting. ``today`` is the anchor; consumers
    should treat it as the timeline's left edge.
    """

    today: date
    past_vests: list[VestMarker]
    future_vests: list[VestMarker]
    life_events: list[LifeEventMarker]
    retire_ready_zones: list[RetireZone]


# Codex IMPORTANT (commit #10 review): horizon_days must be clamped to
# a positive, bounded range. Negative values would create a past horizon
# silently zeroing future vests; very large values would expand the
# projection cost in ways MAX_FUTURE_VESTS only partially neutralizes.
MIN_HORIZON_DAYS = 1
MAX_HORIZON_DAYS = 365 * 50  # 50y — more than a human plan horizon


def build_holistic_timeline(
    session: Session,
    user_id: str,
    *,
    horizon_days: int = 365 * 30,  # 30y default
    as_of: date | None = None,
) -> HolisticTimeline:
    """Compose past vests + projected future vests + life events + the
    retire-ready-age zones into one payload for the timeline card.

    Returns layered markers sorted by date so the UI can render them
    chronologically without sorting itself.

    Empty users return a well-formed payload with empty marker arrays
    and ``today`` set to ``as_of or date.today()`` -- the route layer
    surfaces this directly (the UI shows an "ingest a Schwab CSV"
    nudge).

    Performance note (codex IMPORTANT, commit #10 review): the retire-
    ready-zone composition calls ``effective_retire_ready_age()`` once
    per scenario (bear / base / bull); each call runs ``project_cashflow``
    independently. The whole thing therefore does 3x the projection
    work. Acceptable for v1 since the timeline is fetched on page-load
    rather than per-keystroke, and the underlying compute is still <2s
    on a real household. A follow-on commit can batch the scenarios
    once a shared ``project_all_scenarios`` helper lands; for now the
    correctness invariant (every consumer calls the canonical clamp)
    matters more than the perf duplication.
    """
    if horizon_days < MIN_HORIZON_DAYS:
        horizon_days = MIN_HORIZON_DAYS
    elif horizon_days > MAX_HORIZON_DAYS:
        horizon_days = MAX_HORIZON_DAYS

    today = as_of or date.today()
    horizon = today + timedelta(days=horizon_days)

    past_vests = _load_past_vests(session, user_id, today)
    future_vests = _project_future_vests(
        session=session,
        user_id=user_id,
        today=today,
        horizon=horizon,
    )
    # Canonical fallback: the rsu_vest_events table is empty for the
    # primary household (vests live in identity_yaml.rsu_vest_schedule,
    # not the CSV-ingested table). When the DB-backed projection yields
    # nothing, project the forward vest calendar from the SAME canonical
    # source the overview RSU chapter reads. Display-only; never wired
    # into fi_crossing / savings.
    if not future_vests:
        future_vests = _future_vests_from_canonical_schedule(
            session=session,
            user_id=user_id,
            today=today,
            horizon=horizon,
        )
    life_events = _load_life_events(session, user_id)
    # Canonical fallback: when no dated life_events rows exist, surface
    # the deterministic spending-phase boundaries from the canonical
    # phase-expense curve as point-in-time markers, anchored to the
    # household's current-age axis.
    if not life_events:
        life_events = _life_events_from_phase_curve(
            session=session,
            user_id=user_id,
            today=today,
            horizon=horizon,
        )
    retire_ready_zones = _build_retire_ready_zones(
        session=session,
        user_id=user_id,
        today=today,
    )

    return HolisticTimeline(
        today=today,
        past_vests=past_vests,
        future_vests=future_vests,
        life_events=life_events,
        retire_ready_zones=retire_ready_zones,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_past_vests(
    session: Session,
    user_id: str,
    today: date,
) -> list[VestMarker]:
    """Read ``rsu_vest_events`` rows with ``vest_date <= today``,
    return sorted ascending by date.
    """
    rows = (
        session.query(RsuVestEvent)
        .filter(RsuVestEvent.user_id == user_id)
        .filter(RsuVestEvent.vest_date <= today)
        .order_by(RsuVestEvent.vest_date.asc())
        .all()
    )
    out: list[VestMarker] = []
    for r in rows:
        shares = float(r.shares_vested)
        fmv = float(r.fmv_per_share_usd) if r.fmv_per_share_usd is not None else None
        gross = shares * fmv if fmv is not None else None
        out.append(VestMarker(
            kind="past_vest",
            date=r.vest_date,
            symbol=r.symbol,
            grant_id=r.grant_id,
            shares=shares,
            fmv_per_share_usd=fmv,
            estimated_gross_usd=gross,
        ))
    return out


def _project_future_vests(
    *,
    session: Session,
    user_id: str,
    today: date,
    horizon: date,
) -> list[VestMarker]:
    """Project upcoming vests by iterating +90d from the latest historical
    vest forward until horizon OR MAX_FUTURE_VESTS, whichever comes first.

    v1 heuristic: per-tranche share count := latest_vest.shares_vested,
    per-share FMV := latest_vest.fmv_per_share_usd. A grant-aware
    follow-on (read per-grant cadence + per-grant tranche size) is
    documented in the spec as a future-improvement bullet.

    Returns empty list when the user has no historical vests at all
    (no cadence to project from).
    """
    latest = (
        session.query(RsuVestEvent)
        .filter(RsuVestEvent.user_id == user_id)
        .order_by(RsuVestEvent.vest_date.desc())
        .first()
    )
    if latest is None:
        return []

    shares = float(latest.shares_vested)
    fmv = (
        float(latest.fmv_per_share_usd)
        if latest.fmv_per_share_usd is not None
        else None
    )
    gross = shares * fmv if fmv is not None else None

    out: list[VestMarker] = []
    projected = latest.vest_date
    while len(out) < MAX_FUTURE_VESTS:
        projected = projected + timedelta(days=_QUARTERLY_VEST_DAYS)
        if projected <= today:
            # Walk forward through any historical gap so the first
            # emitted marker lands strictly in the future.
            continue
        if projected > horizon:
            break
        out.append(VestMarker(
            kind="future_vest",
            date=projected,
            symbol=latest.symbol,
            grant_id=latest.grant_id,
            shares=shares,
            fmv_per_share_usd=fmv,
            estimated_gross_usd=gross,
        ))
    return out


def _future_vests_from_canonical_schedule(
    *,
    session: Session,
    user_id: str,
    today: date,
    horizon: date,
) -> list[VestMarker]:
    """Forward RSU vest markers from the canonical ``rsu_vest_schedule``.

    Reuses ``rsu_savings.project_quarterly_vests`` over the identity's
    ``active_grants`` + ``quarterly_vests`` (portal-authoritative) — the
    SAME source + call shape as the overview RSU chapter
    (``overview_assembler._rsu_year_rows``), so the per-year share/NIS
    buckets reconcile to the shekel. Each emitted marker carries the
    NVDA spot (``implied_nvda_price_usd``) and a display-only estimated
    gross/net so the tooltip reads in dollars.

    Display-only — NOT wired into fi_crossing / savings. Degrades to an
    empty list (with a logged reason) on any missing source or coercion
    failure; never raises.
    """
    try:
        from argosy.services.rsu_savings import project_quarterly_vests
        from argosy.services.wealth_dashboard import _load_user_context_yaml
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("timeline future_vests: rsu modules unavailable: %s", exc)
        return []

    try:
        user_ctx = _load_user_context_yaml(session, user_id)
    except Exception as exc:
        logger.warning("timeline future_vests: user context load failed: %s", exc)
        return []
    if not isinstance(user_ctx, dict):
        return []
    sched = user_ctx.get("rsu_vest_schedule")
    if not isinstance(sched, dict):
        logger.info("timeline future_vests: no rsu_vest_schedule in identity_yaml")
        return []
    active_grants = sched.get("active_grants")
    portal_vests = sched.get("quarterly_vests")
    if not active_grants:
        logger.info("timeline future_vests: no active_grants in rsu_vest_schedule")
        return []

    nvda_price = sched.get("implied_nvda_price_usd")
    try:
        nvda_price_f = float(nvda_price) if nvda_price is not None else None
    except (TypeError, ValueError):
        nvda_price_f = None

    try:
        events = project_quarterly_vests(
            active_grants,
            portal_vests,
            horizon_start_year=today.year,
            horizon_years=5,
        )
    except Exception as exc:
        logger.warning("timeline future_vests: vest projection failed: %s", exc)
        return []

    out: list[VestMarker] = []
    for ev in events:
        d_raw = ev.get("date")
        try:
            vest_date = date.fromisoformat(str(d_raw)[:10])
        except (TypeError, ValueError):
            continue
        if vest_date <= today or vest_date > horizon:
            continue
        try:
            shares = float(ev.get("shares"))
        except (TypeError, ValueError):
            continue
        gross = shares * nvda_price_f if nvda_price_f is not None else None
        # Display-only net estimate (mirrors the overview chapter's
        # retention multiplier); kept in the gross field's sibling so the
        # tooltip can show an at-vest figure without a separate field.
        out.append(VestMarker(
            kind="future_vest",
            date=vest_date,
            symbol="NVDA",
            grant_id="rsu_vest_schedule",
            shares=shares,
            fmv_per_share_usd=nvda_price_f,
            estimated_gross_usd=gross,
        ))
    if not out:
        logger.info("timeline future_vests: no canonical vests within horizon")
    return out


def _life_events_from_phase_curve(
    *,
    session: Session,
    user_id: str,
    today: date,
    horizon: date,
) -> list[LifeEventMarker]:
    """Spending-phase life-event markers from the canonical phase curve.

    Reuses ``retirement.phase_expenses.build_phase_expense_curve`` — the
    SAME source the overview phase-timeline chapter reads. Each phase's
    ``start_age`` is translated to a calendar date using the household's
    ``current_age_years`` anchor (so the markers sit on the same date
    axis as vests). Only phase boundaries that fall on/after today and
    within the horizon are emitted (a phase the household is already in,
    e.g. kids_peak for a 44-yo, has a past start and is skipped).

    Category is set to ``expense_event`` so the frontend renders a
    rose down-marker (these are spending-phase shifts). Degrades to an
    empty list (with a logged reason) on any missing source; never raises.
    """
    try:
        from argosy.services.retirement.phase_expenses import (
            build_phase_expense_curve,
        )
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("timeline life_events: phase_expenses unavailable: %s", exc)
        return []

    try:
        household = extract_household_state(session, user_id, today=today)
    except Exception as exc:
        logger.warning("timeline life_events: household state failed: %s", exc)
        return []
    current_age = getattr(household, "current_age_years", None)
    if current_age is None:
        logger.info("timeline life_events: no current_age_years anchor")
        return []

    try:
        phases = build_phase_expense_curve()
    except Exception as exc:
        logger.warning("timeline life_events: phase curve failed: %s", exc)
        return []

    out: list[LifeEventMarker] = []
    for ph in phases:
        months_to = int(round((ph.start_age - float(current_age)) * 12.0))
        if months_to < 0:
            # Household is already past this phase's start — skip (no
            # future point-in-time marker for a phase already entered).
            continue
        start_date = _add_months(today, months_to)
        if start_date < today or start_date > horizon:
            continue
        mult = getattr(getattr(ph, "monthly_multiplier", None), "value", None)
        desc = (
            f"{ph.label.replace('_', ' ')} phase "
            f"(ages {ph.start_age}-{ph.end_age}"
            + (f", {float(mult):.2f}x baseline" if mult is not None else "")
            + ")"
        )
        out.append(LifeEventMarker(
            date=start_date,
            category="expense_event",
            kind=ph.label,
            amount_usd=None,
            description=desc,
        ))
    if not out:
        logger.info("timeline life_events: no phase boundaries within horizon")
    return out


def _load_life_events(
    session: Session,
    user_id: str,
) -> list[LifeEventMarker]:
    """Read ``life_events`` rows that have a target_date set, sorted
    ascending by date.

    Rows without a target_date are recurring patterns (e.g.
    recurring_expense entries) and are handled by a separate code path
    -- they don't render as point-in-time timeline markers.
    """
    rows = (
        session.query(LifeEvent)
        .filter(LifeEvent.user_id == user_id)
        .filter(LifeEvent.target_date.isnot(None))
        .order_by(LifeEvent.target_date.asc())
        .all()
    )
    out: list[LifeEventMarker] = []
    for r in rows:
        amount = float(r.amount_usd) if r.amount_usd is not None else None
        # The .isnot(None) filter above means target_date is guaranteed
        # non-null here; the mypy hint reflects that.
        assert r.target_date is not None
        out.append(LifeEventMarker(
            date=r.target_date,
            category=r.category,
            kind=r.kind,
            amount_usd=amount,
            description=r.description,
        ))
    return out


def _build_retire_ready_zones(
    *,
    session: Session,
    user_id: str,
    today: date,
) -> list[RetireZone]:
    """Compute the three retire-ready scenarios via the canonical clamp
    function and translate each into a (age, date) zone marker.

    Scenarios whose ``age_years`` is None (no crossing within horizon)
    are skipped -- the UI will show "no crossing in 30y" elsewhere if
    needed.
    """
    household = extract_household_state(session, user_id, today=today)
    zones: list[RetireZone] = []
    for scenario in ("bear", "base", "bull"):
        rr: EffectiveRetireReadyAge = effective_retire_ready_age(
            scenario,  # type: ignore[arg-type]
            user_id,
            session,
            as_of=today,
        )
        if rr.age_years is None:
            continue
        # Translate age->date using household's current_age_years anchor.
        months_to = int(round(
            (rr.age_years - household.current_age_years) * 12.0
        ))
        expected = _add_months(today, max(0, months_to))
        zones.append(RetireZone(
            scenario=scenario,  # type: ignore[arg-type]
            age_years=rr.age_years,
            expected_date=expected,
            clamp_reason=rr.clamp_reason,
        ))
    return zones
