"""The period directive — the team's ONE assembled "here's your move" object.

This is the *Assemble* step of the proactive loop (SDD §1.6 / the design spec):
it composes the two halves of a period's allocation into a single verdict —

  * BUY  — idle cash over target, deployed via the ONE canonical engine
           (core sleeves + the discovery-sourced high-potential sleeve).
  * SELL — the NVDA glide *policy* sell (surface-or-stay-quiet).

— stamped with a freshness record. Both the inbox card and the Step-3 directive
loop render the SAME object, so the buy and the sell can never disagree across
surfaces.

Freshness contract: read-only by default. With ``refresh=True`` (the on-demand
"wait while I refresh" path) it refreshes stale FX *before* advising — never
advise on stale data — and records that it did. Refreshing discovery (running the
funnel) is expensive and stays with the Step-3 loop; its staleness is *flagged*
here, not force-refreshed.

The collaborator calls are wrapped in thin module-level seams (``_detect_cash``,
``build_buy_list``, ``_assess_sell``, ``_refresh_fx``, ``_discovery_stale_days``)
so the composition is unit-testable without a seeded DB.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from argosy.services.nvda_policy_sell import NvdaPolicySell

_log = logging.getLogger(__name__)

# Discovery picks older than this are considered stale for advising.
_DISCOVERY_STALE_DAYS = 7


@dataclass(frozen=True)
class PeriodDirective:
    generated_at: str
    buy: list[dict[str, Any]]
    buy_excess_usd: float
    buy_headline: str
    sell: NvdaPolicySell
    freshness: dict[str, Any] = field(default_factory=dict)

    @property
    def has_actions(self) -> bool:
        return bool(self.buy) or self.sell.status == "sell_due"

    def to_dict(self) -> dict[str, Any]:
        """The client projection: one grouped 'your move this period' payload."""
        return {
            "generated_at": self.generated_at,
            "has_actions": self.has_actions,
            "buy": {
                "excess_usd": round(self.buy_excess_usd, 2),
                "headline": self.buy_headline,
                "items": self.buy,
            },
            "sell": {
                "status": self.sell.status,
                "category": self.sell.category,
                "headline": self.sell.headline,
                "tranche_nis": self.sell.tranche_nis,
                "nvda_current_pct": self.sell.nvda_current_pct,
                "nvda_cap_pct": self.sell.nvda_cap_pct,
                "n_quarters": self.sell.n_quarters,
                "tax_note": self.sell.tax_note,
                "notes": list(self.sell.notes),
            },
            "freshness": self.freshness,
        }


# --------------------------------------------------------------------------
# Collaborator seams (thin; patched in tests)
# --------------------------------------------------------------------------


def _detect_cash(db, user_id: str, today: date):
    from argosy.services.unallocated_cash_detector import detect_unallocated_cash_overage

    return detect_unallocated_cash_overage(db, user_id=user_id, today=today)


def _assess_sell(db, user_id: str, today: date) -> NvdaPolicySell:
    from argosy.services.nvda_policy_sell import assess_nvda_policy_sell

    return assess_nvda_policy_sell(session=db, user_id=user_id, today=today)


def _refresh_fx(db) -> bool:
    from argosy.services.fx import refresh_if_stale

    return refresh_if_stale(db, currencies=("USD",), max_stale_days=1)


def _fx_is_stale(db) -> bool:
    from argosy.services.fx import is_fx_stale

    return is_fx_stale(db, currencies=("USD",), max_stale_days=1)


def _discovery_stale_days(db, user_id: str, today: date) -> int | None:
    """Age (days) of the cached discovery picks, or ``None`` when unknown.

    ``_load_discovery_state`` returns an ISO *string* (or None) for ``last``; parse
    it defensively (a datetime/date is also tolerated) rather than subtracting a
    string from a date.
    """
    from argosy.api.routes.portfolio import _load_discovery_state

    _picks, _estimated, last = _load_discovery_state(user_id)
    if last is None:
        return None
    if isinstance(last, str):
        dt = datetime.fromisoformat(last)
    elif isinstance(last, datetime):
        dt = last
    else:
        return (today - last).days  # already a date
    # Normalize an aware datetime to UTC before taking the date, so a non-UTC
    # offset can't shift the day and mis-count staleness by one.
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return (today - dt.date()).days


def build_buy_list(db, user_id: str, excess_usd: float, today: date) -> list[dict[str, Any]] | None:
    """The buy list from the ONE canonical engine ``/deploy-cash`` uses (core
    sleeves + discovery-sourced high-potential sleeve + diversifier redirect).
    Returns ``None`` on any failure so the caller falls back gracefully.
    """
    try:
        from argosy.config import get_settings
        from argosy.services.allocation_engine import tradeable_holdings
        from argosy.services.deployment_funnel.canonical import (
            build_canonical_deploy_plan,
            deploy_plan_to_buy_list,
        )
        from argosy.services.portfolio_snapshot_store import (
            get_latest_snapshot_row,
            row_to_snapshot,
        )
        from argosy.services.target_allocation_doc import load_plan_target_allocation
        from argosy.state.queries import get_current_plan

        pv = get_current_plan(db, user_id)
        doc = load_plan_target_allocation(pv) if pv is not None else None
        if doc is None:
            return None

        row = get_latest_snapshot_row(db, user_id=user_id)
        snap = row_to_snapshot(row) if row is not None else None
        holdings: dict[str, float] = {}
        snapshot_prices: dict[str, float] = {}
        if snap is not None:
            holdings, _cash = tradeable_holdings(snap)
            for p in getattr(snap, "positions", []) or []:
                sym = (getattr(p, "symbol", "") or "").strip().upper()
                px = getattr(p, "current_price", None)
                if sym and px:
                    snapshot_prices[sym] = float(px)

        plan, _result = build_canonical_deploy_plan(
            doc=doc, holdings=holdings, cash_usd=excess_usd,
            deploy_amount_usd=excess_usd, as_of=today, use_high_potential=True,
            user_id=user_id, snapshot_prices=snapshot_prices,
            funnel_enabled=get_settings().deployment_funnel_enabled,
            session=db,
        )
        return deploy_plan_to_buy_list(plan, doc, user_id=user_id, session=db)
    except Exception:  # noqa: BLE001 — never break the directive on the buy build
        _log.exception("period_directive.buy_list_failed", extra={"user_id": user_id})
        return None


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def assemble_period_directive(
    *, db, user_id: str, today: date | None = None, refresh: bool = False
) -> PeriodDirective:
    """Compose the buy + sell halves into the period directive.

    ``refresh=True`` refreshes stale FX before advising (the on-demand path).
    """
    today = today or datetime.now(timezone.utc).date()

    # Freshness gate: when a refresh is requested we refresh FX BEFORE advising so
    # the buy/sell are computed on current data. ``fx_stale`` is read from the
    # ACTUAL cache state after the attempt (not inferred from the refresh boolean,
    # which returns False for both "already fresh" and "fetch failed") — so a
    # BoI-down refresh that leaves the cache stale is flagged, never silently clean.
    refreshed = False
    if refresh:
        try:
            refreshed = bool(_refresh_fx(db))
        except Exception:  # noqa: BLE001 — last-known FX stands; staleness read below
            _log.warning("period_directive.fx_refresh_failed", extra={"user_id": user_id})
    try:
        fx_stale = _fx_is_stale(db)
    except Exception:  # noqa: BLE001 — fail-CLOSED: unknown freshness is treated as
        # stale so the directive is never silently reported clean.
        _log.warning("period_directive.fx_stale_read_failed", extra={"user_id": user_id})
        fx_stale = True

    discovery_stale_days: int | None = None
    try:
        discovery_stale_days = _discovery_stale_days(db, user_id, today)
    except Exception:  # noqa: BLE001 — freshness is advisory, never blocks the directive
        discovery_stale_days = None

    # BUY half. Isolated: a detector failure (e.g. a malformed stored snapshot)
    # degrades to no buy half rather than 500-ing the directive.
    buy: list[dict[str, Any]] = []
    buy_excess = 0.0
    buy_headline = ""
    try:
        event = _detect_cash(db, user_id, today)
    except Exception:  # noqa: BLE001 — never break the directive on the cash detector
        _log.exception("period_directive.detect_cash_failed", extra={"user_id": user_id})
        event = None
    if event is not None and event.excess_usd > 0:
        buy_excess = round(float(event.excess_usd), 2)
        buy_headline = event.headline or ""
        rows = build_buy_list(db, user_id, buy_excess, today)
        if rows is None:
            # Fallback: the detector's own plan-bound proposals (no discovery sleeve).
            rows = [
                {
                    "instrument": p.instrument,
                    "asset_class": p.asset_class,
                    "amount_usd": round(p.amount_usd, 2),
                    "rationale": p.rationale,
                }
                for p in event.proposals
            ]
        buy = rows

    # SELL half. Isolated the same way: degrade to no_action rather than 500.
    try:
        sell = _assess_sell(db, user_id, today)
    except Exception:  # noqa: BLE001 — never break the directive on the sell assessor
        _log.exception("period_directive.assess_sell_failed", extra={"user_id": user_id})
        sell = NvdaPolicySell(
            status="no_action", category="policy", tranche_nis=0.0,
            nvda_current_pct=0.0, nvda_cap_pct=0.0, n_quarters=0,
            headline="Sell check unavailable this period.", tax_note="",
        )

    return PeriodDirective(
        generated_at=datetime.now(timezone.utc).isoformat(),
        buy=buy,
        buy_excess_usd=buy_excess,
        buy_headline=buy_headline,
        sell=sell,
        freshness={
            "refreshed": refreshed,
            "fx_stale": fx_stale,
            "discovery_stale_days": discovery_stale_days,
            "discovery_stale": (
                discovery_stale_days is not None
                and discovery_stale_days > _DISCOVERY_STALE_DAYS
            ),
        },
    )


__all__ = ["PeriodDirective", "assemble_period_directive", "build_buy_list"]
