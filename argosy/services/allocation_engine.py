"""Deterministic allocation engine — plan-bound, glide-aware, no LLM.

Turns the canonical TargetAllocationDoc + current holdings/cash into executable
buy/trim/swap candidates. All amounts are computed here; the Slice-1b agent only
ranks/sequences/explains these candidates and invents no numbers.

The candidate/leg value objects are the cross-phase contracts — defined once in
``argosy.services.contracts`` and re-exported here so existing imports
(``from argosy.services.allocation_engine import AllocationCandidate``) keep
working.
"""
from __future__ import annotations

import enum
from datetime import date

from argosy.logging import get_logger
from argosy.services.contracts import AllocationCandidate, AllocationLeg

log = get_logger(__name__)


class AllocationMode(str, enum.Enum):
    PURE_REBALANCE = "pure_rebalance"
    CASH_ONLY_DEPLOY = "cash_only_deploy"
    REBALANCE_PLUS_CASH = "rebalance_plus_cash"


# Documented UCITS domicile swaps (S18). old US-domiciled symbol -> UCITS twin.
REPLACES_SYMBOLS: dict[str, str] = {
    "VOO": "CSPX", "SCHD": "FUSA", "VEA": "EXUS", "SCHG": "R1GR",
    "USMV": "SPMV", "VNQ": "DPYA", "SGOV": "IB01", "VGSH": "IBTA",
}


# Sentinel symbol for book percentage that sits in a glide label with no named
# instrument class (e.g. the transitional "Individual Stocks (non-NVDA, to
# redeploy)" band). Surfaced — never silently dropped — so target values always
# conserve to the book total. Callers that deploy cash exclude this bucket (you
# cannot BUY an unnamed instrument); it represents legacy holdings being wound
# down, not a buy target.
UNMAPPED_BUCKET = "__UNMAPPED__"


def class_targets_as_of(doc, as_of: date) -> dict[str, float]:
    """Class-label -> target % as of ``as_of`` along the glide.

    Picks the latest glide waypoint dated on-or-before ``as_of`` (so a mid-
    transition date uses the CURRENT composition, not the end-state). When
    ``as_of`` precedes every waypoint, uses the first. Falls back to the final
    class targets when the doc carries no glide.
    """
    glide = list(getattr(doc, "glide", []) or [])
    if glide:
        glide.sort(key=lambda w: w.date)
        chosen = glide[0]
        for wp in glide:
            if wp.date <= as_of:
                chosen = wp
            else:
                break
        return dict(chosen.composition_pct_by_class)
    return {c.label: c.target_pct for c in doc.classes}


def target_values_by_symbol(doc, total: float, as_of: date) -> dict[str, float]:
    """symbol -> target USD value, using glide-aware class %s and per-instrument
    weights. A symbol in >1 class is summed.

    Conservation (codex #1): the glide-aware class percentages are the authority,
    so they must sum to ~100; a glide label with no matching instrument class is
    surfaced under :data:`UNMAPPED_BUCKET` rather than dropped, so the returned
    values always sum to ``total``."""
    class_pct = class_targets_as_of(doc, as_of)
    pct_sum = sum(class_pct.values())
    if abs(pct_sum - 100.0) > 0.5:
        raise ValueError(
            f"glide-aware class percentages sum to {pct_sum:.2f}, not ~100 "
            f"(as_of={as_of}); refusing to size targets off a non-conserving plan")
    by_label = {c.label: c for c in doc.classes}
    out: dict[str, float] = {}
    for label, pct in class_pct.items():
        frac = pct / 100.0
        cls = by_label.get(label)
        if cls is None:
            # Transitional/redeploy band with no named instruments — keep it
            # explicit so the book total is conserved.
            out[UNMAPPED_BUCKET] = round(out.get(UNMAPPED_BUCKET, 0.0) + frac * total, 2)
            continue
        for instr in cls.instruments:
            v = frac * (instr.weight_within_class_pct / 100.0) * total
            out[instr.symbol] = round(out.get(instr.symbol, 0.0) + v, 2)
    return out


_CASH_TYPES = {"cash", "money_market", "mmf"}


def tradeable_holdings(snapshot) -> tuple[dict[str, float], float]:
    """(holdings_by_symbol_usd, total_cash_usd) from a PortfolioSnapshot.

    Filters the cash sentinel ("-"/blank) and cash-typed rows out of holdings
    and aggregates them into total_cash_usd. Symbols are upper-cased + summed.
    Account/currency splitting is deferred (v1 needs only the total for the
    cash-deploy math)."""
    holdings: dict[str, float] = {}
    cash = 0.0
    for p in getattr(snapshot, "positions", []) or []:
        sym = (getattr(p, "symbol", "") or "").strip().upper()
        usd_k = getattr(p, "usd_value_k", None) or 0.0
        usd = float(usd_k) * 1000.0
        asset_type = (getattr(p, "asset_type", "") or "").lower()
        if asset_type in _CASH_TYPES:
            cash += usd
            continue
        if not sym or sym == "-":
            cash += usd  # blank-symbol rows are cash lines
            continue
        if usd == 0.0:
            continue
        holdings[sym] = round(holdings.get(sym, 0.0) + usd, 2)
    return holdings, round(cash, 2)


def cash_only_deploy(doc, holdings: dict[str, float], cash_usd: float, *,
                     as_of: date, account_id: str = "ibkr",
                     currency: str = "USD") -> list[AllocationCandidate]:
    """Buy-only, cash-constrained deployment toward the glide-aware targets.

    Targets are computed on the POST-deploy book (current + cash). Each
    under-target symbol's gap = max(0, target_value - current). Cash is
    deployed to gaps; if total gap exceeds cash, it is rationed pro-rata to the
    gaps (water-fill). NEVER emits a trim; buys sum to min(total_gap, cash).
    Returns one BUY candidate per funded symbol, largest first. The unmapped/
    redeploy bucket is never a buy target (you cannot buy an unnamed instrument).
    """
    if cash_usd <= 0:
        return []
    post_book = round(sum(holdings.values()) + cash_usd, 2)
    targets = target_values_by_symbol(doc, post_book, as_of)
    targets.pop(UNMAPPED_BUCKET, None)
    gaps = {sym: max(0.0, tv - holdings.get(sym, 0.0)) for sym, tv in targets.items()}
    gaps = {s: g for s, g in gaps.items() if g > 0.0}
    total_gap = sum(gaps.values())
    if total_gap <= 0.0:
        return []
    scale = 1.0 if total_gap <= cash_usd else cash_usd / total_gap
    # Largest gaps first; a running budget absorbs per-leg rounding so the buys
    # can never sum to MORE than cash (codex bug 1 — independent rounding of N
    # legs of cash/N overshoots by cents).
    remaining = round(cash_usd, 2)
    out: list[AllocationCandidate] = []
    for sym, gap in sorted(gaps.items(), key=lambda kv: (-kv[1], kv[0])):
        amount = min(round(gap * scale, 2), remaining)
        if amount <= 0.0:
            continue
        remaining = round(remaining - amount, 2)
        out.append(AllocationCandidate(
            kind="BUY",
            legs=(AllocationLeg(side="BUY", symbol=sym, account_id=account_id,
                                currency=currency, notional_usd=amount,
                                funding_source="cash"),),
            horizon="now",
            rationale=f"Deploy ${amount:,.0f} cash into {sym} toward its plan target.",
            cites=(f"plan_target:{sym}",),
        ))
    out.sort(key=lambda c: -c.total_notional_usd)
    return out


def _named_deltas(doc, holdings: dict[str, float], *, as_of: date,
                  keep_band_pct: float, extra_cash: float = 0.0
                  ) -> tuple[dict[str, float], dict[str, float]]:
    """(adds, trims) in USD toward the glide-aware targets on the book
    ``sum(holdings) + extra_cash``.

    Glide semantics are honoured directly off :func:`target_values_by_symbol`
    (NOT a class-target override + diff): a glide label missing from the
    waypoint contributes 0, and the UNMAPPED/redeploy band is respected —
    holdings that are NOT a named plan instrument (legacy singles being wound
    down) are kept up to the band's current glide value and only their
    COLLECTIVE excess is trimmed, never force-exited (codex bugs 2 & 3)."""
    total = round(sum(holdings.values()) + extra_cash, 2)
    if total <= 0:
        return {}, {}
    targets = target_values_by_symbol(doc, total, as_of)
    unmapped_target = targets.pop(UNMAPPED_BUCKET, 0.0)
    named = set(targets)
    band = total * keep_band_pct / 100.0

    adds: dict[str, float] = {}
    trims: dict[str, float] = {}
    for sym in named:
        delta = targets[sym] - holdings.get(sym, 0.0)
        if abs(delta) < band:
            continue
        if delta > 0:
            adds[sym] = round(delta, 2)
        else:
            trims[sym] = round(-delta, 2)

    # Legacy / unmapped holdings (not a named target) collectively belong to the
    # redeploy band; trim only their excess over the band's current glide value.
    legacy = {s: v for s, v in holdings.items() if s not in named}
    total_legacy = sum(legacy.values())
    excess = total_legacy - unmapped_target
    if total_legacy > 0 and excess > band:
        scale = excess / total_legacy
        for s, v in legacy.items():
            amt = round(v * scale, 2)
            if amt > 0:
                trims[s] = round(trims.get(s, 0.0) + amt, 2)
    return adds, trims


def _pair_into_candidates(adds: dict[str, float], trims: dict[str, float], *,
                          account_id: str, currency: str,
                          buy_funding: str = "trim_proceeds"
                          ) -> list[AllocationCandidate]:
    """Pair a trim with its UCITS-replacement buy into one SWAP (decrementing
    BOTH legs by the matched amount so residuals are never dropped), then emit
    leftover trims/buys as standalone candidates."""
    trim_rem = dict(trims)
    add_rem = dict(adds)
    out: list[AllocationCandidate] = []
    for old_sym in list(trim_rem):
        new_sym = REPLACES_SYMBOLS.get(old_sym)
        if not new_sym or new_sym not in add_rem:
            continue
        notional = round(min(trim_rem[old_sym], add_rem[new_sym]), 2)
        if notional <= 0:
            continue
        out.append(AllocationCandidate(
            kind="SWAP",
            legs=(
                AllocationLeg(side="SELL", symbol=old_sym, account_id=account_id,
                              currency=currency, notional_usd=notional,
                              funding_source="trim_proceeds"),
                AllocationLeg(side="BUY", symbol=new_sym, account_id=account_id,
                              currency=currency, notional_usd=notional,
                              funding_source="trim_proceeds"),
            ),
            horizon="this_quarter",
            rationale=f"Domicile swap {old_sym}→{new_sym} (UCITS); size-matched.",
            cites=(f"plan_target:{new_sym}", f"replaces:{old_sym}"),
        ))
        trim_rem[old_sym] = round(trim_rem[old_sym] - notional, 2)
        add_rem[new_sym] = round(add_rem[new_sym] - notional, 2)

    for sym, amt in trim_rem.items():
        if amt <= 0:
            continue
        out.append(AllocationCandidate(
            kind="TRIM",
            legs=(AllocationLeg(side="SELL", symbol=sym, account_id=account_id,
                                currency=currency, notional_usd=round(amt, 2),
                                funding_source="trim_proceeds"),),
            horizon="this_quarter",
            rationale=f"Trim ${amt:,.0f} of {sym} toward its plan target.",
            cites=(f"plan_target:{sym}",)))
    for sym, amt in add_rem.items():
        if amt <= 0:
            continue
        out.append(AllocationCandidate(
            kind="BUY",
            legs=(AllocationLeg(side="BUY", symbol=sym, account_id=account_id,
                                currency=currency, notional_usd=round(amt, 2),
                                funding_source=buy_funding),),
            horizon="this_quarter",
            rationale=f"Add ${amt:,.0f} of {sym} toward its plan target.",
            cites=(f"plan_target:{sym}",)))
    out.sort(key=lambda c: -c.total_notional_usd)
    return out


def rebalance_candidates(doc, holdings: dict[str, float], *, as_of: date,
                         account_id: str = "ibkr", currency: str = "USD",
                         keep_band_pct: float = 1.0) -> list[AllocationCandidate]:
    """Closed-book rebalance candidates from the glide-aware plan, pairing a
    trim with its UCITS-replacement buy into a single SWAP where the
    REPLACES_SYMBOLS map applies. Legacy holdings in the unmapped/redeploy band
    are held at the band's glide weight (only collective excess is trimmed)."""
    adds, trims = _named_deltas(doc, holdings, as_of=as_of,
                                keep_band_pct=keep_band_pct)
    return _pair_into_candidates(adds, trims, account_id=account_id,
                                 currency=currency)


def compute_allocation(doc, holdings: dict[str, float], mode: AllocationMode, *,
                       cash_usd: float = 0.0, as_of: date | None = None,
                       account_id: str = "ibkr") -> list[AllocationCandidate]:
    """Dispatch to the requested mode. ``as_of`` defaults to today."""
    from datetime import date as _date
    when = as_of or _date.today()
    if mode == AllocationMode.CASH_ONLY_DEPLOY:
        return cash_only_deploy(doc, holdings, cash_usd, as_of=when, account_id=account_id)
    if mode == AllocationMode.PURE_REBALANCE:
        return rebalance_candidates(doc, holdings, as_of=when, account_id=account_id)
    # REBALANCE_PLUS_CASH: a SINGLE closed-book computation on (holdings + cash)
    # — not a sequential deploy-then-rebalance, which would buy an instrument and
    # then trim part of it back when the targets are recomputed on a smaller book
    # (codex bug 3). Cash funds the residual buys; trims fund swaps.
    adds, trims = _named_deltas(doc, holdings, as_of=when, keep_band_pct=1.0,
                               extra_cash=cash_usd)
    return _pair_into_candidates(adds, trims, account_id=account_id,
                                 currency="USD", buy_funding="cash")


__all__ = [
    "AllocationMode", "AllocationLeg", "AllocationCandidate", "REPLACES_SYMBOLS",
    "UNMAPPED_BUCKET", "class_targets_as_of", "target_values_by_symbol",
    "tradeable_holdings", "cash_only_deploy", "rebalance_candidates",
    "compute_allocation",
]
