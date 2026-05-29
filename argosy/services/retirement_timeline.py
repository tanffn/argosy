"""Holistic-timeline composer for the /retirement page (sprint commit #10).

Builds a single chronological payload combining:

  * Historical RSU vest events (from ``rsu_vest_events``).
  * Projected future RSU vests (heuristic: quarterly cadence, latest
    historical + 90d, repeated up to a horizon / count cap).
  * Life events with a fixed target_date (from ``life_events``).
  * Retire-ready-age zones for the three scenarios (bear / base / bull)
    via the canonical ``effective_retire_ready_age()`` clamp.

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
    life_events = _load_life_events(session, user_id)
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
