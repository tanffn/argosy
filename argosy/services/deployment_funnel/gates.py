from __future__ import annotations

import math
from dataclasses import dataclass

from argosy.services.contracts import AllocationCandidate
from argosy.services.deployment_funnel.contracts import (
    CandidateStatus,
    HistoryFeatures,
)
from argosy.services.deployment_funnel.look_through import effective_nvda_usd
from argosy.services.deployment_funnel.reserve import CASH_LIKE_SYMBOLS


def _floor_pct(numer: float, denom: float) -> float:
    """Percentage floored to 1 decimal — never rounds UP past the limit, so a
    capped size can never exceed the headroom that produced it (codex M3)."""
    if denom <= 0:
        return 0.0
    return max(0.0, math.floor(numer / denom * 1000) / 10.0)


@dataclass(frozen=True)
class GateInputs:
    current_effective_nvda_usd: float
    book_usd: float
    nvda_cap_pct: float
    reserve_shortfall_usd: float
    plan_classes: frozenset[str]
    class_of: dict[str, str]


def classify_candidate(
    cand: AllocationCandidate,
    symbol: str,
    history: HistoryFeatures,
    news_sentiment: str | None,
    gi: GateInputs,
) -> tuple[CandidateStatus, str, float | None]:
    """Deterministic status for one candidate. Order matters: fail-closed on
    stale data first; then plan-gap; then reserve duplication; then the
    look-through concentration cap. Price HISTORY features never gate here."""
    notional = cand.total_notional_usd

    # 1. Fail-closed on stale market data.
    if history.stale:
        return (
            CandidateStatus.DEFER,
            "market quote stale — deferring rather than acting blind",
            None,
        )

    # 2. Plan-gap: a class the plan doesn't contain must go through a plan change.
    cls = gi.class_of.get(symbol.upper())
    if cls is not None and cls not in gi.plan_classes:
        return (
            CandidateStatus.REQUIRES_PLAN_CHANGE,
            f"'{cls}' is not a sleeve in the current plan — raise a plan "
            f"change before buying",
            None,
        )

    # 3. Reserve duplication: no net-new cash-like beyond the reserve shortfall.
    if symbol.upper() in CASH_LIKE_SYMBOLS:
        if gi.reserve_shortfall_usd <= 0.0:
            return (
                CandidateStatus.VETO,
                "reserve already funded — no added T-bills/cash",
                None,
            )
        if notional > gi.reserve_shortfall_usd:
            # Reserve needs only part of this candidate (codex M4).
            cap_pct = _floor_pct(gi.reserve_shortfall_usd, notional)
            return (
                CandidateStatus.CAP_AT_PCT,
                f"cap {symbol} at {cap_pct:.1f}% — reserve only needs "
                f"${gi.reserve_shortfall_usd:,.0f} more, not ${notional:,.0f}",
                cap_pct,
            )
        # Within the shortfall → a legitimate reserve top-up; falls through.

    # 4. Concentration cap via look-through (effective NVDA, not nominal).
    add_nvda = effective_nvda_usd(symbol, notional)
    if add_nvda > 0.0:
        cap_usd = gi.book_usd * gi.nvda_cap_pct / 100.0
        headroom = cap_usd - gi.current_effective_nvda_usd
        if headroom <= 0.0:
            return (
                CandidateStatus.VETO,
                f"buying {symbol} adds ${add_nvda:,.0f} NVDA via index "
                f"look-through; effective NVDA already over the "
                f"{gi.nvda_cap_pct:.0f}% cap",
                None,
            )
        if add_nvda > headroom:
            cap_pct = _floor_pct(headroom, add_nvda)
            return (
                CandidateStatus.CAP_AT_PCT,
                f"cap {symbol} at {cap_pct:.1f}% — full size adds "
                f"${add_nvda:,.0f} NVDA via look-through, over the cap",
                cap_pct,
            )

    return (CandidateStatus.APPROVE, "fills a plan sleeve within caps", None)
