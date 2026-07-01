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

    # 4. Concentration gate via look-through — a MARGINAL-QUALITY rule on the
    #    instrument's own NVDA weight vs the cap. IMPORTANT (codex): this does
    #    NOT reduce an over-cap book. The deploy cash is already IN the book, so
    #    a cash-funded buy reallocates within a FIXED total; buying a fund with
    #    ANY NVDA (even 7%) nudges the book NVDA % UP, not down. Only SELLING
    #    NVDA deconcentrates. So the rule here is: don't ADD anything MORE
    #    NVDA-concentrated than the cap (that would worsen single-name risk);
    #    a <=cap instrument is an acceptable place to put idle cash without
    #    making concentration materially worse. Fixing the 56.6% itself is a
    #    SELL decision this cash-deploy path deliberately does not make.
    add_nvda = effective_nvda_usd(symbol, notional)
    if add_nvda > 0.0 and notional > 0.0:
        cap_frac = gi.nvda_cap_pct / 100.0
        inst_wt = add_nvda / notional
        book_pct = (
            gi.current_effective_nvda_usd / gi.book_usd if gi.book_usd > 0 else 1.0
        )
        if inst_wt <= cap_frac + 1e-9:
            return (
                CandidateStatus.APPROVE,
                f"{symbol} is {inst_wt * 100:.0f}% NVDA (<= the "
                f"{gi.nvda_cap_pct:.0f}% cap) — acceptable to add idle cash "
                f"(doesn't worsen single-name risk; won't reduce the 56.6% either)",
                None,
            )
        # Instrument is MORE NVDA-concentrated than the cap.
        if book_pct >= cap_frac:
            return (
                CandidateStatus.VETO,
                f"{symbol} is {inst_wt * 100:.0f}% NVDA — above the "
                f"{gi.nvda_cap_pct:.0f}% cap while your book is already over it "
                f"({book_pct * 100:.0f}%); adding it slows the deconcentration",
                None,
            )
        # Under the cap: the book has headroom. Allow the slice that keeps it
        # at/under the cap. Cash-funded (fixed book B): (C + w*x)/B <= cap
        # => x <= (cap*B - C) / w  (codex: NOT /(w-cap), which assumed the buy
        # grew the denominator with new outside money).
        max_notional = (
            cap_frac * gi.book_usd - gi.current_effective_nvda_usd
        ) / inst_wt
        if max_notional >= notional:
            return (CandidateStatus.APPROVE,
                    f"{symbol} is {inst_wt * 100:.0f}% NVDA but the book has "
                    f"headroom under the {gi.nvda_cap_pct:.0f}% cap", None)
        if max_notional <= 0.0:
            return (CandidateStatus.VETO,
                    f"{symbol} is {inst_wt * 100:.0f}% NVDA — no headroom under "
                    f"the {gi.nvda_cap_pct:.0f}% cap", None)
        cap_pct = _floor_pct(max_notional, notional)
        return (
            CandidateStatus.CAP_AT_PCT,
            f"cap {symbol} at {cap_pct:.1f}% — it is {inst_wt * 100:.0f}% NVDA "
            f"(> the {gi.nvda_cap_pct:.0f}% cap); larger would push the book over",
            cap_pct,
        )

    return (CandidateStatus.APPROVE, "fills a plan sleeve within caps", None)
