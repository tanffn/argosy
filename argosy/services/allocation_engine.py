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
    out: list[AllocationCandidate] = []
    for sym, gap in gaps.items():
        amount = round(gap * scale, 2)
        if amount <= 0.0:
            continue
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


def rebalance_candidates(doc, holdings: dict[str, float], *, as_of: date,
                         account_id: str = "ibkr", currency: str = "USD",
                         keep_band_pct: float = 1.0) -> list[AllocationCandidate]:
    """Closed-book rebalance candidates from the glide-aware plan, pairing a
    trim with its UCITS-replacement buy into a single SWAP where the
    REPLACES_SYMBOLS map applies."""
    from argosy.services.plan_proposal_diff import diff_plan_vs_holdings

    # Build a glide-adjusted doc view by overriding class target_pct with the
    # as-of waypoint; diff_plan_vs_holdings reads class.target_pct.
    pct = class_targets_as_of(doc, as_of)
    adj_classes = [c.model_copy(update={"target_pct": pct.get(c.label, c.target_pct)})
                   for c in doc.classes]
    adj_doc = doc.model_copy(update={"classes": adj_classes})

    deltas = diff_plan_vs_holdings(adj_doc, holdings, keep_band_pct=keep_band_pct)
    adds = {d.symbol: d for d in deltas if d.action == "add"}
    trims = {d.symbol: d for d in deltas if d.action == "trim"}

    # Remaining (unpaired) magnitudes — DECREMENT by the paired amount so the
    # residual is never dropped (codex: min() must not discard the larger leg).
    trim_rem = {d.symbol: abs(d.delta_value_usd) for d in trims.values()}
    add_rem = {d.symbol: abs(d.delta_value_usd) for d in adds.values()}
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

    # Residual trims / buys (incl. the larger side of an unequal swap).
    for sym, amt in trim_rem.items():
        if amt <= 0:
            continue
        out.append(AllocationCandidate(
            kind="TRIM",
            legs=(AllocationLeg(side="SELL", symbol=sym, account_id=account_id,
                                currency=currency, notional_usd=round(amt, 2),
                                funding_source="trim_proceeds"),),
            horizon="this_quarter", rationale=trims[sym].rationale,
            cites=(f"plan_target:{sym}",)))
    for sym, amt in add_rem.items():
        if amt <= 0:
            continue
        out.append(AllocationCandidate(
            kind="BUY",
            legs=(AllocationLeg(side="BUY", symbol=sym, account_id=account_id,
                                currency=currency, notional_usd=round(amt, 2),
                                funding_source="trim_proceeds"),),
            horizon="this_quarter", rationale=adds[sym].rationale,
            cites=(f"plan_target:{sym}",)))
    out.sort(key=lambda c: -c.total_notional_usd)
    return out


__all__ = [
    "AllocationMode", "AllocationLeg", "AllocationCandidate", "REPLACES_SYMBOLS",
    "UNMAPPED_BUCKET", "class_targets_as_of", "target_values_by_symbol",
    "tradeable_holdings", "cash_only_deploy", "rebalance_candidates",
]
