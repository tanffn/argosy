from __future__ import annotations

import math
from dataclasses import dataclass

from argosy.services.contracts import AllocationCandidate
from argosy.services.deployment_funnel.contracts import (
    CandidateFlag,
    CandidateStatus,
    HistoryFeatures,
)
from argosy.services.deployment_funnel.look_through import (
    effective_nvda_usd,
    has_lookthrough,
)
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


def candidate_flags(
    cand: AllocationCandidate,
    symbol: str,
    gi: GateInputs,
) -> tuple[CandidateFlag, ...]:
    """Deterministic FACTS about one candidate that warrant fleet judgment — no
    verdicts. This is the primary signal the fleet adjudicates (approve / trim /
    veto). A candidate with no flags is a clean plan-fill needing no judgment.

    The engine only reconciles against the plan's own numbers here; whether a
    flagged fact should stop or shrink the buy is the fleet's call, not a
    hardcoded threshold's. (Class-not-in-plan is handled as a hard invariant in
    classify_candidate, not here — it's a routing fact, not a judgment.)"""
    notional = cand.total_notional_usd
    sym = symbol.upper()
    flags: list[CandidateFlag] = []

    # Reserve overfund — a cash-like buy beyond what the plan's reserve target
    # still needs. Whether extra buffer is wanted (conflict risk etc.) is a
    # judgment, so surface the fact, don't veto.
    if sym in CASH_LIKE_SYMBOLS and notional > gi.reserve_shortfall_usd:
        over = round(notional - max(0.0, gi.reserve_shortfall_usd), 2)
        flags.append(CandidateFlag(
            kind="reserve_overfund",
            materiality="medium" if gi.reserve_shortfall_usd <= 0 else "low",
            fact=(
                f"{symbol} is cash-like; the plan's reserve target needs only "
                f"${max(0.0, gi.reserve_shortfall_usd):,.0f} more, so ${over:,.0f} "
                f"of this buy adds to an already-funded reserve"
            ),
            detail={"notional_usd": notional,
                    "reserve_shortfall_usd": gi.reserve_shortfall_usd,
                    "overfund_usd": over},
        ))

    # Concentration — any NVDA look-through is a fact about single-name risk on a
    # book already over the cap. Density vs the cap sets materiality.
    add_nvda = effective_nvda_usd(symbol, notional)
    if add_nvda > 0.0 and notional > 0.0:
        inst_wt = add_nvda / notional
        cap_frac = gi.nvda_cap_pct / 100.0
        book_pct = (
            gi.current_effective_nvda_usd / gi.book_usd if gi.book_usd > 0 else 1.0
        )
        denser = inst_wt > cap_frac + 1e-9
        flags.append(CandidateFlag(
            kind="denser_than_cap" if denser else "nvda_lookthrough",
            materiality="high" if denser else ("medium" if book_pct >= cap_frac else "low"),
            fact=(
                f"{symbol} adds {inst_wt * 100:.0f}% NVDA look-through "
                f"(${add_nvda:,.0f}); book is {book_pct * 100:.0f}% NVDA vs the "
                f"{gi.nvda_cap_pct:.0f}% single-name cap"
                + (" — DENSER than the cap" if denser else "")
            ),
            detail={"instrument_nvda_pct": round(inst_wt * 100, 1),
                    "book_nvda_pct": round(book_pct * 100, 1),
                    "cap_pct": gi.nvda_cap_pct,
                    "add_nvda_usd": add_nvda},
        ))

    # Unverified look-through — a non-cash symbol with no look-through entry: its
    # 0 NVDA is an assumption, not a fact. Surface it (concentration unverified).
    if not has_lookthrough(sym) and sym not in CASH_LIKE_SYMBOLS:
        flags.append(CandidateFlag(
            kind="unverified_lookthrough",
            materiality="low",
            fact=(
                f"{symbol} has no look-through entry — its NVDA content is "
                f"unverified (assumed 0); concentration may be under-counted"
            ),
            detail={},
        ))

    return tuple(flags)


def classify_candidate(
    cand: AllocationCandidate,
    symbol: str,
    history: HistoryFeatures,
    news_sentiment: str | None,
    gi: GateInputs,
) -> tuple[CandidateStatus, str, float | None]:
    """Conservative FAIL-SAFE fallback disposition for one candidate, used when
    the agent fleet doesn't run. It is NOT the primary decision path any more —
    ``candidate_flags`` produces the facts and the fleet decides approve/trim/veto.
    This function only survives so the system degrades gracefully (a sensible,
    plan-reconciled default) when the fleet is unavailable; it never OVERRIDES a
    fleet verdict. Order: plan-gap (hard invariant) first; then reserve; then the
    look-through concentration read. Price HISTORY features never gate here."""
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
    #    SCOPE (important): the cap is a SINGLE-NAME DENSITY limit. This gate
    #    enforces "no NEW buy denser in NVDA than the cap without review" — it does
    #    NOT try to force post-trade TOTAL book NVDA <= cap when the book already
    #    starts over the cap (a cash buy can't reduce an existing over-weight; only
    #    SELLING NVDA does). So a diversified fund at/below cap density APPROVES
    #    even on an over-cap book; refusing it would just strand cash.
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
        if inst_wt <= cap_frac + 1e-9:
            # The instrument's own NVDA density is AT/BELOW the plan's single-name
            # cap. Buying a diversified fund with SOME NVDA (e.g. CSPX ~7%, FUSA
            # ~6%) cannot make single-name density worse than the cap the plan
            # already accepts, and it is exactly what the plan targets. The book's
            # over-cap concentration is reduced by SELLING NVDA, not by refusing
            # diversified equity and stranding cash. Plan-compliant -> APPROVE.
            return (
                CandidateStatus.APPROVE,
                f"{symbol} is {inst_wt * 100:.0f}% NVDA look-through — within the "
                f"{gi.nvda_cap_pct:.0f}% single-name cap; a plan-compliant "
                f"diversified buy",
                None,
            )
        # Instrument is DENSER in NVDA than the cap (e.g. R1GR ~14%, direct NVDA).
        if book_pct >= cap_frac:
            # Book already at/over the cap AND the instrument is more NVDA-dense
            # than the cap: whether to add it now vs deconcentrate/reserve is a
            # genuine risk judgment the cap number can't answer -> route to fleet.
            return (
                CandidateStatus.NEEDS_FLEET_REVIEW,
                f"{symbol} is {inst_wt * 100:.0f}% NVDA look-through — DENSER than "
                f"the {gi.nvda_cap_pct:.0f}% cap, and the book is already "
                f"{book_pct * 100:.0f}% NVDA (over the cap). Adding a >cap-dense "
                f"instrument here is a risk-officer judgment, routed to the fleet",
                None,
            )
        # Book has headroom: PURE arithmetic reconcile of a >cap-dense instrument —
        # cash-funded (fixed book B): (C + w*x)/B <= cap  =>  x <= (cap*B - C)/w.
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
            f"cap {symbol} at {cap_pct:.1f}% — it is {inst_wt * 100:.0f}% NVDA "
            f"(> the {gi.nvda_cap_pct:.0f}% cap); larger would push the book over",
            cap_pct,
        )

    return (CandidateStatus.APPROVE, "fills a plan sleeve within caps", None)
