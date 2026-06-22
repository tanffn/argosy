"""RSU pre-vest planner — three-scenario tax outlook for upcoming vests.

Sprint #2 commit #12 (per spec
``docs/superpowers/specs/2026-05-29-anomaly-detection-rsu-prevest-design.md``
§3). Forward-looking, purely advisory: no flag rows, no monitor entries —
the UI consumes it directly as a card on /retirement.

The historical-data contract:
  * ``rsu_vest_events`` (migration 0044) carries the realized vests.
  * Future vests are PROJECTED per grant by stepping forward at
    ``_CADENCE_DAYS`` (90d) intervals from the latest vest for each
    grant. Each grant is independently projected and capped at
    ``MAX_PROJECTED_VESTS_PER_GRANT``.

For each projected vest we compute three tax-rate scenarios (codex
IMPORTANT #4 — surface the assumption-sensitivity instead of hiding
behind a single opaque rate):

  * ``rate_nominal``        := plan-assumed marginal top rate (or 0.42 fallback)
  * ``rate_effective``      := observed prior-year effective filed rate
                               (or 0.30 fallback — best-case scenario)
  * ``rate_conservative``   := max(0.47, nominal + 0.05) — capped supplemental
                               withholding worst-case

Plus an allocation preview using the NOMINAL post-tax amount as the
budget for the canonical ``cash_only_deploy`` engine against the current
accepted plan + holdings (the SAME path /deploy-cash uses). The preview is
empty when no plan is accepted — better an honest empty than a fabricated
split.

The NVDA spot price is pulled from the latest portfolio snapshot's
positions block (same pattern as
``wealth_dashboard._build_rsu_income_block``); falls back to the latest
historical vest's FMV when the snapshot has no NVDA row.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

from sqlalchemy.orm import Session

from argosy.services.portfolio_snapshot_store import (
    get_latest_snapshot_row,
    row_to_snapshot,
)
from argosy.services.retirement.reference import resolve
from argosy.services.retirement.windfall_allocator import (
    AllocationProposal,
    _allocate_long_term_from_plan,
)
from argosy.state.models import RsuVestEvent


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hard cap on the number of projected vests we emit per grant. 4 ~ one
# year of quarterly tranches; past that the cadence heuristic stops
# being meaningfully accurate.
MAX_PROJECTED_VESTS_PER_GRANT = 4

# Fallback marginal top rate when the user's plan has no
# ``tax.marginal_top_rate`` override. 0.42 reflects a high-earner
# Israeli bracket without the top-tier surcharge — intentionally lower
# than ``DEFAULT_MARGINAL_TOP_RATE`` (0.47) so it reads as a plausible
# nominal rather than a worst-case. Conservatism is added on top via
# ``DEFAULT_CONSERVATIVE_FLOOR`` and the +0.05 bump.
DEFAULT_NOMINAL_RATE = 0.42

# Fallback effective rate when no prior-year tax-analyst observation
# exists. 0.30 reflects an aggregate effective bracket including
# credits / treaty offsets — the "best-case" scenario.
DEFAULT_EFFECTIVE_RATE = 0.30

# Conservative-scenario floor. Caps the supplemental-withholding-rate
# worst case at 0.47 even if the nominal rate is below it.
DEFAULT_CONSERVATIVE_FLOOR = 0.47

# Cadence between projected per-grant vests. Matches the heuristic in
# ``argosy.services.cashflow_projection`` and
# ``argosy.services.retirement_timeline`` (NVDA quarterly grants).
_CADENCE_DAYS = 90


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpcomingVest:
    """One projected upcoming vest + three-scenario tax estimates."""

    grant_id: str
    expected_vest_date: date
    days_until: int
    shares_projected: float
    nvda_price_usd: float  # latest spot or FMV fallback
    expected_gross_usd: float

    # Three-scenario tax estimate per codex IMPORTANT #4.
    rate_nominal: float
    rate_effective: float
    rate_conservative: float
    expected_post_tax_nominal_usd: float
    expected_post_tax_effective_usd: float
    expected_post_tax_conservative_usd: float

    # Allocation preview built off the NOMINAL post-tax amount.
    allocation_preview: list[AllocationProposal] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "grant_id": self.grant_id,
            "expected_vest_date": self.expected_vest_date.isoformat(),
            "days_until": self.days_until,
            "shares_projected": round(self.shares_projected, 4),
            "nvda_price_usd": round(self.nvda_price_usd, 4),
            "expected_gross_usd": round(self.expected_gross_usd, 2),
            "rate_nominal": round(self.rate_nominal, 4),
            "rate_effective": round(self.rate_effective, 4),
            "rate_conservative": round(self.rate_conservative, 4),
            "expected_post_tax_nominal_usd": round(
                self.expected_post_tax_nominal_usd, 2
            ),
            "expected_post_tax_effective_usd": round(
                self.expected_post_tax_effective_usd, 2
            ),
            "expected_post_tax_conservative_usd": round(
                self.expected_post_tax_conservative_usd, 2
            ),
            "allocation_preview": [
                p.to_dict() for p in self.allocation_preview
            ],
        }


@dataclass(frozen=True)
class UpcomingVestOutlook:
    """Composite payload returned by :func:`compute_upcoming_vest_outlook`."""

    user_id: str
    as_of: date
    horizon_days: int
    upcoming: list[UpcomingVest]
    # Rates used for the headline scenario, surfaced once at the top
    # level so the UI footnote doesn't have to read it off the first
    # row (which may be missing entirely).
    rate_nominal: float
    rate_effective: float
    rate_conservative: float

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "as_of": self.as_of.isoformat(),
            "horizon_days": self.horizon_days,
            "upcoming": [u.to_dict() for u in self.upcoming],
            "rate_nominal": round(self.rate_nominal, 4),
            "rate_effective": round(self.rate_effective, 4),
            "rate_conservative": round(self.rate_conservative, 4),
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_upcoming_vest_outlook(
    session: Session,
    user_id: str,
    *,
    horizon_days: int = 90,
    as_of: date | None = None,
) -> UpcomingVestOutlook:
    """Project the next ``horizon_days`` of expected vests + tax outlook.

    Per-grant projection: for each distinct ``grant_id`` with at least
    one historical vest, project forward at ``_CADENCE_DAYS`` intervals
    from the latest vest date. Cap each grant at
    ``MAX_PROJECTED_VESTS_PER_GRANT``. Returns the merged list sorted
    ascending by ``expected_vest_date``.

    Three tax rates per upcoming vest (codex IMPORTANT #4):

        rate_nominal       = plan-assumed marginal rate (or 0.42 fallback)
        rate_effective     = observed prior-year effective rate
                             (or 0.30 fallback)
        rate_conservative  = max(0.47, rate_nominal + 0.05)

    The allocation preview uses the NOMINAL post-tax amount as the
    budget for the canonical ``cash_only_deploy`` engine against the
    accepted plan + holdings (empty when no plan is accepted).
    """
    if as_of is None:
        as_of = date.today()
    horizon_end = as_of + timedelta(days=horizon_days)

    nominal = _resolve_nominal_rate(session, user_id)
    effective = _resolve_effective_rate(session, user_id)
    conservative = max(DEFAULT_CONSERVATIVE_FLOOR, nominal + 0.05)

    nvda_price = _spot_nvda_price(session, user_id)

    # Canonical plan + current holdings for the allocation preview, loaded via
    # the SAME accessors /deploy-cash uses so the preview's instruments match.
    # Best-effort: an empty preview (no accepted plan / no snapshot) is honest;
    # this advisory card must not 500 when a plan isn't accepted yet.
    from argosy.services.allocation_engine import tradeable_holdings
    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.queries import get_current_plan

    pv = get_current_plan(session, user_id)
    plan_doc = load_plan_target_allocation(pv) if pv is not None else None
    prev_holdings: dict[str, float] = {}
    _snap_row = get_latest_snapshot_row(session, user_id=user_id)
    if _snap_row is not None:
        prev_holdings, _ = tradeable_holdings(row_to_snapshot(_snap_row))

    projected: list[UpcomingVest] = []
    for latest in _latest_vest_per_grant(session, user_id):
        per_share_fallback = float(latest.fmv_per_share_usd)
        per_share = nvda_price if nvda_price is not None else per_share_fallback
        shares = float(latest.shares_vested)
        projections = _project_grant_dates(
            latest.vest_date,
            as_of=as_of,
            horizon_end=horizon_end,
        )
        for vest_date in projections:
            gross = shares * per_share
            post_nominal = gross * (1.0 - nominal)
            post_effective = gross * (1.0 - effective)
            post_conservative = gross * (1.0 - conservative)
            preview = _build_allocation_preview(
                post_nominal, plan_doc, prev_holdings, as_of=as_of,
            )
            projected.append(UpcomingVest(
                grant_id=latest.grant_id,
                expected_vest_date=vest_date,
                days_until=max(0, (vest_date - as_of).days),
                shares_projected=shares,
                nvda_price_usd=per_share,
                expected_gross_usd=gross,
                rate_nominal=nominal,
                rate_effective=effective,
                rate_conservative=conservative,
                expected_post_tax_nominal_usd=post_nominal,
                expected_post_tax_effective_usd=post_effective,
                expected_post_tax_conservative_usd=post_conservative,
                allocation_preview=preview,
            ))

    projected.sort(key=lambda u: u.expected_vest_date)

    return UpcomingVestOutlook(
        user_id=user_id,
        as_of=as_of,
        horizon_days=horizon_days,
        upcoming=projected,
        rate_nominal=nominal,
        rate_effective=effective,
        rate_conservative=conservative,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _latest_vest_per_grant(
    session: Session, user_id: str
) -> list[RsuVestEvent]:
    """Return one RsuVestEvent per distinct grant_id (the latest one).

    Reads everything for the user (cheap; the table is per-vest-event
    not per-tick) and folds by grant_id in Python. Avoids a SQL window
    function which sqlite supports but is a pain to write twice.
    """
    rows: Iterable[RsuVestEvent] = (
        session.query(RsuVestEvent)
        .filter(RsuVestEvent.user_id == user_id)
        .order_by(RsuVestEvent.vest_date.desc())
        .all()
    )
    seen: dict[str, RsuVestEvent] = {}
    for r in rows:
        # First write wins because we ordered by vest_date DESC, so the
        # first row per grant_id is the most recent.
        if r.grant_id not in seen:
            seen[r.grant_id] = r
    return list(seen.values())


def _project_grant_dates(
    latest_vest_date: date,
    *,
    as_of: date,
    horizon_end: date,
) -> list[date]:
    """Step forward at _CADENCE_DAYS until horizon_end OR MAX cap.

    Skips dates that have already passed (latest historical vest may be
    weeks old, but the FIRST projected date we emit must be strictly
    after ``as_of``).
    """
    out: list[date] = []
    projected = latest_vest_date
    while len(out) < MAX_PROJECTED_VESTS_PER_GRANT:
        projected = projected + timedelta(days=_CADENCE_DAYS)
        if projected <= as_of:
            continue
        if projected > horizon_end:
            break
        out.append(projected)
    return out


def _resolve_nominal_rate(session: Session, user_id: str) -> float:
    """Read ``tax.marginal_top_rate`` from the reference resolver.

    Same key the tax_engine uses (see
    ``argosy.services.retirement.tax_engine._marginal_rate``). Falls
    back to ``DEFAULT_NOMINAL_RATE`` when the resolver throws or
    returns a non-numeric value.

    Note: the resolver looks at identity_yaml first, then citations.
    If the user's plan has an override we get it; otherwise we get the
    reference default (which is 0.47 — the "real" top bracket). For the
    nominal scenario we WANT a plausible mid-rate, so we use our own
    fallback (0.42) when no explicit override is present, but happily
    pick up an explicit user-set override when one exists.
    """
    try:
        v = resolve("tax.marginal_top_rate", user_id=user_id, session=session)
        if isinstance(v.value, (int, float)):
            return float(v.value)
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_NOMINAL_RATE


def _resolve_effective_rate(session: Session, user_id: str) -> float:
    """Best-case prior-year effective rate.

    The tax_analyst agent emits an effective rate per filed-return run
    but we don't have a stable cross-run accessor yet (the field lives
    under ``identity_yaml.tax_history.prior_year_effective_rate`` in
    some user contexts; it's not always present). Falls back to
    ``DEFAULT_EFFECTIVE_RATE`` (0.30) when missing.
    """
    try:
        v = resolve(
            "tax.prior_year_effective_rate",
            user_id=user_id,
            session=session,
        )
        if isinstance(v.value, (int, float)):
            return float(v.value)
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_EFFECTIVE_RATE


def _spot_nvda_price(session: Session, user_id: str) -> float | None:
    """Latest NVDA spot price from the snapshot's positions_json (None when
    missing). The long-term allocation preview is now sized off the canonical
    plan doc + holdings, not the snapshot's TSV allocation table."""
    row = get_latest_snapshot_row(session, user_id=user_id)
    if row is None:
        return None

    nvda_price: float | None = None
    try:
        positions = json.loads(row.positions_json or "[]")
    except (ValueError, TypeError):
        positions = []
    for p in positions:
        if isinstance(p, dict) and (p.get("symbol") or "").upper() == "NVDA":
            price = p.get("current_price")
            if price:
                try:
                    nvda_price = float(price)
                except (ValueError, TypeError):
                    nvda_price = None
                break
    return nvda_price


def _build_allocation_preview(
    post_tax_usd: float,
    doc,
    holdings: dict[str, float],
    *,
    as_of: date,
) -> list[AllocationProposal]:
    """Plan-bound long-term allocation preview for the nominal post-tax amount.

    Routes through the canonical ``cash_only_deploy`` engine (via
    ``_allocate_long_term_from_plan``) so the preview's instruments match
    /deploy-cash + the windfall/unallocated-cash buy lists.

    Empty when:
      * post-tax amount is non-positive (defensive — vest gross was zero)
      * no current canonical plan is accepted (``doc is None``) — an honest
        empty preview beats a fabricated split.
    """
    if post_tax_usd <= 0:
        return []
    if doc is None:
        return []
    proposals, _remaining = _allocate_long_term_from_plan(
        post_tax_usd, doc, holdings, as_of=as_of,
    )
    return proposals


__all__ = [
    "DEFAULT_CONSERVATIVE_FLOOR",
    "DEFAULT_EFFECTIVE_RATE",
    "DEFAULT_NOMINAL_RATE",
    "MAX_PROJECTED_VESTS_PER_GRANT",
    "UpcomingVest",
    "UpcomingVestOutlook",
    "compute_upcoming_vest_outlook",
]
