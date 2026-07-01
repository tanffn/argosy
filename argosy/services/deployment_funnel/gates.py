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

    # NOTE: a stale/missing price does NOT gate here. The deterministic verdict
    # (plan-gap / reserve / concentration cap) is computed from SHARE NOTIONAL ×
    # index look-through — no price needed — so refusing to act on a missing
    # quote would wrongly hold cash for a price-independent decision. Staleness
    # is carried on `history.stale` as a FEATURE for the price-dependent
    # judgment layer (gold-at-ATH etc.), not a gate. (Was a hard DEFER; that
    # held ~$60k of a $100k deploy purely because bare UCITS tickers don't
    # resolve on yfinance — an artifact, not a real objection.)

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

    # 4. Concentration via look-through. This gate does ONE deterministic job:
    #    reconcile a cash-funded buy against the plan's OWN NVDA cap number. It
    #    does NOT encode an investment judgment about single-name risk quality.
    #
    #    A cash-funded buy reallocates within a FIXED book (cash -> fund doesn't
    #    change the total), so adding ANY NVDA look-through nudges the book NVDA %
    #    UP. Two regimes:
    #      * Book UNDER the plan cap  -> pure arithmetic: fill until the RESULTING
    #        book NVDA % reaches the cap; cap the slice that would overshoot. This
    #        derives entirely from the plan's cap number — no judgment.
    #      * Book AT/OVER the plan cap -> whether to add MORE NVDA-correlated
    #        exposure now (or instead sell to deconcentrate / park in reserve /
    #        route only to zero-NVDA diversifiers) is an investment JUDGMENT the
    #        cap number alone can't answer. ROUTE it to the fleet — do NOT invent
    #        an approve/veto here. (Fixing the concentration itself is a SELL
    #        decision this cash-deploy path never makes on its own.)
    #    A zero-NVDA instrument (add_nvda == 0) is a pure diversifier and skips
    #    this gate entirely (falls through to APPROVE).
    add_nvda = effective_nvda_usd(symbol, notional)
    if add_nvda > 0.0 and notional > 0.0:
        cap_frac = gi.nvda_cap_pct / 100.0
        inst_wt = add_nvda / notional
        book_pct = (
            gi.current_effective_nvda_usd / gi.book_usd if gi.book_usd > 0 else 1.0
        )
        if book_pct >= cap_frac:
            # At/over the plan cap: a genuine judgment call — route to the fleet.
            return (
                CandidateStatus.NEEDS_FLEET_REVIEW,
                f"book is {book_pct * 100:.0f}% NVDA (look-through), at/over the "
                f"{gi.nvda_cap_pct:.0f}% plan cap; {symbol} adds "
                f"{inst_wt * 100:.0f}% NVDA-correlated exposure — whether to add "
                f"more now vs deconcentrate/reserve is a risk-officer judgment, "
                f"routed to the fleet",
                None,
            )
        # Book has headroom under the plan cap: PURE arithmetic reconcile.
        # Cash-funded (fixed book B): (C + w*x)/B <= cap  =>  x <= (cap*B - C)/w.
        max_notional = (
            cap_frac * gi.book_usd - gi.current_effective_nvda_usd
        ) / inst_wt
        if max_notional >= notional:
            return (CandidateStatus.APPROVE,
                    f"{symbol} keeps the book within the {gi.nvda_cap_pct:.0f}% "
                    f"NVDA plan cap", None)
        cap_pct = _floor_pct(max_notional, notional)
        return (
            CandidateStatus.CAP_AT_PCT,
            f"cap {symbol} at {cap_pct:.1f}% — larger would push the book past "
            f"the {gi.nvda_cap_pct:.0f}% NVDA plan cap",
            cap_pct,
        )

    return (CandidateStatus.APPROVE, "fills a plan sleeve within caps", None)
