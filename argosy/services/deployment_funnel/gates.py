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

    # 4. Concentration cap via look-through — measured on the RESULTING NVDA
    #    PERCENTAGE, not absolute dollars. Key fix: when the book is already
    #    over the cap (56.6% vs 13%), the way DOWN is to buy assets whose NVDA
    #    weight is BELOW the current book %, which DILUTES the concentration. A
    #    buy that lowers (or holds) the NVDA % must be allowed even though it
    #    adds some absolute NVDA — vetoing it (the old bug) rejected the very
    #    diversifying buys that reduce concentration and stranded the cash.
    add_nvda = effective_nvda_usd(symbol, notional)
    if add_nvda > 0.0 and gi.book_usd > 0.0 and notional > 0.0:
        cap_frac = gi.nvda_cap_pct / 100.0
        pre_pct = gi.current_effective_nvda_usd / gi.book_usd
        post_pct = (gi.current_effective_nvda_usd + add_nvda) / (gi.book_usd + notional)
        # Allowed if the buy leaves NVDA within the cap, OR (while over the cap)
        # does not RAISE the NVDA share — i.e. it deconcentrates or holds.
        if post_pct <= cap_frac or post_pct <= pre_pct + 1e-9:
            return (
                CandidateStatus.APPROVE,
                (
                    f"fills a plan sleeve; {symbol} is "
                    f"{add_nvda / notional * 100:.0f}% NVDA (< book {pre_pct * 100:.0f}%) "
                    f"so it DILUTES concentration ({pre_pct * 100:.1f}%→{post_pct * 100:.1f}%)"
                    if pre_pct > cap_frac
                    else "fills a plan sleeve within the NVDA cap"
                ),
                None,
            )
        # The buy RAISES the NVDA share while over the cap → concentrating.
        return (
            CandidateStatus.VETO,
            f"buying {symbol} RAISES NVDA {pre_pct * 100:.1f}%→{post_pct * 100:.1f}% "
            f"(its {add_nvda / notional * 100:.0f}% NVDA weight exceeds your book) — "
            f"deepens the concentration being unwound",
            None,
        )

    return (CandidateStatus.APPROVE, "fills a plan sleeve within caps", None)
