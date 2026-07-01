from __future__ import annotations

from dataclasses import dataclass

from argosy.services.contracts import AllocationCandidate
from argosy.services.deployment_funnel.contracts import (
    CandidateStatus,
    HistoryFeatures,
)
from argosy.services.deployment_funnel.look_through import effective_nvda_usd
from argosy.services.deployment_funnel.reserve import CASH_LIKE_SYMBOLS


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

    # 3. Reserve duplication: no net-new cash-like when the reserve is funded.
    if symbol.upper() in CASH_LIKE_SYMBOLS and gi.reserve_shortfall_usd <= 0.0:
        return (
            CandidateStatus.VETO,
            "reserve already funded — no added T-bills/cash",
            None,
        )

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
            cap_pct = max(0.0, round(headroom / add_nvda * 100, 1))
            return (
                CandidateStatus.CAP_AT_PCT,
                f"cap {symbol} at {cap_pct:.0f}% — full size adds "
                f"${add_nvda:,.0f} NVDA via look-through, over the cap",
                cap_pct,
            )

    return (CandidateStatus.APPROVE, "fills a plan sleeve within caps", None)
