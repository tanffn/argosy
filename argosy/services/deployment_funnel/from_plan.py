"""Adapt a `DeploymentPlan` (candidate generator output) + the user's plan doc
and holdings into a deterministic preflight run. This is the glue Task 7 wires
into `GET /api/portfolio/deploy-cash` behind the kill switch."""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from argosy.services.contracts import AllocationCandidate, AllocationLeg
from argosy.services.deployment_funnel.contracts import PreflightResult
from argosy.services.deployment_funnel.gates import GateInputs
from argosy.services.deployment_funnel.look_through import effective_nvda_usd
from argosy.services.deployment_funnel.preflight import run_preflight
from argosy.services.deployment_funnel.reserve import (
    CASH_LIKE_SYMBOLS,
    existing_cash_like_usd,
    reserve_shortfall_usd,
)

_log = logging.getLogger(__name__)

# The plan class that represents the cash/T-bill reserve (matched by label
# substring so a rename to "Cash & T-bills (incl. ILS tranche)" still resolves).
_RESERVE_LABEL_HINT = "cash & t-bills"


def build_gate_inputs(*, doc, holdings_usd: dict[str, float], cash_usd: float) -> GateInputs:
    """Assemble the deterministic gate inputs from the accepted plan doc + the
    latest holdings. Effective NVDA is the look-through sum over current
    holdings; the reserve target is the plan's cash/T-bills class weight."""
    book_usd = round(sum(holdings_usd.values()) + cash_usd, 2)

    current_nvda = 0.0
    for sym, val in holdings_usd.items():
        current_nvda += effective_nvda_usd(sym, val)
    current_nvda = round(current_nvda, 2)

    plan_classes = frozenset(c.label for c in doc.classes)
    class_of: dict[str, str] = {}
    for c in doc.classes:
        for instr in c.instruments:
            class_of[instr.symbol.upper()] = c.label

    reserve_pct = 0.0
    for c in doc.classes:
        if _RESERVE_LABEL_HINT in c.label.lower():
            reserve_pct += float(c.target_pct)

    # Cash sits outside `holdings_usd` (tradeable_holdings pulls it out), so add
    # it back as a cash-like row for the reserve calculation.
    holdings_for_reserve = dict(holdings_usd)
    holdings_for_reserve["CASH_USD"] = holdings_for_reserve.get("CASH_USD", 0.0) + cash_usd

    shortfall = reserve_shortfall_usd(book_usd, holdings_for_reserve, reserve_pct)

    return GateInputs(
        current_effective_nvda_usd=current_nvda,
        book_usd=book_usd,
        nvda_cap_pct=float(doc.nvda_cap_pct),
        reserve_shortfall_usd=shortfall,
        plan_classes=plan_classes,
        class_of=class_of,
    )


def plan_to_candidates(plan) -> list[AllocationCandidate]:
    """Flatten a DeploymentPlan's tiers into BUY candidates (one leg each)."""
    out: list[AllocationCandidate] = []
    for tier in plan.tiers:
        for line in tier.lines:
            out.append(
                AllocationCandidate(
                    kind="BUY",
                    legs=(
                        AllocationLeg(
                            side="BUY", symbol=line.symbol, account_id="leumi",
                            currency="USD", notional_usd=float(line.amount_usd),
                            funding_source="cash",
                        ),
                    ),
                    horizon="now",
                    rationale=line.rationale,
                )
            )
    return out


class SnapshotOrLiveProvider:
    """Best-effort price provider. Prefers a live quote from the yfinance
    adapter; falls back to a passed-in snapshot price map. A symbol with neither
    yields ``None`` (the gates then DEFER — fail-closed, never act blind).
    History high / z-score are left to Increment 2's EOD enrichment (``None``
    here does NOT mark the candidate stale — only a missing last price does)."""

    def __init__(self, snapshot_prices: dict[str, float] | None = None):
        self._snap = {k.upper(): v for k, v in (snapshot_prices or {}).items()}
        self._live: dict[str, float | None] = {}

    def _live_quote(self, symbol: str) -> float | None:
        if symbol in self._live:
            return self._live[symbol]
        price: float | None = None
        try:
            from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

            q = asyncio.run(YFinanceAdapter().get_quote(symbol))
            price = float(getattr(q, "price", None)) if q is not None else None
        except Exception as exc:  # noqa: BLE001 — best-effort; stale => defer
            _log.info("deploy_funnel.quote_miss", extra={"symbol": symbol, "err": str(exc)})
            price = None
        self._live[symbol] = price
        return price

    def quote(self, symbol: str) -> float | None:
        # Snapshot price is authoritative for symbols we already hold (recent,
        # no network); fall back to a live fetch only for genuinely new symbols.
        s = symbol.upper()
        if s in self._snap:
            return self._snap[s]
        return self._live_quote(s)

    def history_high(self, symbol: str) -> float | None:
        return None

    def zscore(self, symbol: str) -> float | None:
        return None


def run_preflight_for_plan(
    plan,
    *,
    doc,
    holdings_usd: dict[str, float],
    cash_usd: float,
    deployable_usd: float,
    signals_by_symbol: dict[str, str] | None = None,
    snapshot_prices: dict[str, float] | None = None,
    fleet_available: bool = False,
) -> PreflightResult:
    """End-to-end: plan candidates -> gate inputs -> deterministic preflight.

    ``fleet_available`` selects the phase-1 flag-based disposition (see
    ``run_preflight``); default False preserves the legacy fallback behavior."""
    gi = build_gate_inputs(doc=doc, holdings_usd=holdings_usd, cash_usd=cash_usd)
    candidates = plan_to_candidates(plan)
    result = run_preflight(
        candidates,
        symbol_of=lambda c: c.legs[0].symbol,
        gate_inputs=gi,
        provider=SnapshotOrLiveProvider(snapshot_prices),
        signals_by_symbol=signals_by_symbol or {},
        deployable_usd=deployable_usd,
        fleet_available=fleet_available,
    )
    # Merge in PLAN-STRUCTURAL gaps (classes the plan is missing entirely, e.g.
    # gold) — these can't come from candidates since the engine never proposes a
    # class the plan lacks. Owner approves adding the sleeve; then deploy on-plan.
    from dataclasses import replace

    from argosy.services.deployment_funnel.look_through import has_lookthrough
    from argosy.services.deployment_funnel.plan_gaps import detect_missing_classes

    added_gaps = tuple(detect_missing_classes(doc))
    # Surface HELD symbols with no look-through entry (codex): the current-NVDA
    # baseline sums look-through over holdings, so an unmapped held fund silently
    # under-counts NVDA and LOOSENS the cap. Flag it rather than trust the 0.
    unmapped_held = sorted(
        {
            s
            for s in holdings_usd
            if not has_lookthrough(s) and s.upper() not in CASH_LIKE_SYMBOLS
        }
    )
    added_notes: tuple[str, ...] = ()
    if unmapped_held:
        added_notes = (
            "current-NVDA baseline may be UNDER-counted (cap too loose): held "
            + ", ".join(unmapped_held)
            + " have no look-through entry — extend LOOKTHROUGH_MAP",
        )
    if added_gaps or added_notes:
        result = replace(
            result,
            plan_gaps=result.plan_gaps + added_gaps,
            notes=result.notes + added_notes,
        )
    return result


def redirect_overflow_to_diversifiers(plan, result, doc, holdings=None):
    """Cash the funnel won't place in its NATURAL sleeve — an over-cap instrument
    (R1GR at 14% NVDA) or T-bills when the reserve is already funded — is REDIRECTED
    into the plan's OWN zero-NVDA diversifier sleeves (ex-US / EM / real-assets),
    split by their plan-target weight, rather than left as idle cash. This deploys
    the full amount into plan holdings (no gold, no plan change) and nudges the NVDA
    concentration DOWN. Returns (new_plan, redirect_note); a no-op returns the plan
    unchanged. Only redistributes concentration/reserve overflow — a class ABSENT
    from the plan (plan-gap) still needs a real plan change and is NOT invented here.

    Exposure-aware: when ``holdings`` is given, overflow bound for a diversifier
    sleeve that you ALREADY cover with a held estate-safe substitute tops up that
    held fund instead of opening the plan's ticker (same rule as the core deploy)."""
    from dataclasses import replace

    from argosy.services.deployment_advisor import DeploymentLine, EstateTag
    from argosy.services.deployment_funnel.look_through import effective_nvda_usd
    from argosy.services.deployment_funnel.reserve import CASH_LIKE_SYMBOLS

    blocked_kinds = {"denser_than_cap", "reserve_overfund"}
    redirect = 0.0
    drop: set[str] = set()
    for e in result.enriched:
        kinds = {f.kind for f in getattr(e, "flags", ())}
        if kinds & blocked_kinds:
            redirect += float(e.candidate.total_notional_usd)
            drop.add(e.symbol.upper())
    redirect = round(redirect, 2)
    if redirect <= 0.0:
        return plan, ""

    # Plan diversifier sleeves: zero-NVDA, non-cash, with a ticker.
    divs: list[tuple[str, float]] = []
    for c in doc.classes:
        for instr in getattr(c, "instruments", []):
            sym = (getattr(instr, "symbol", "") or "").upper()
            if not sym or sym in CASH_LIKE_SYMBOLS or "NVDA" in sym:
                continue
            if effective_nvda_usd(sym, 1_000_000.0) == 0.0:
                divs.append((sym, float(getattr(c, "target_pct", 0.0) or 0.0)))
    if not divs:
        return plan, ""  # nothing plan-compliant to redirect into

    # Exposure-aware remap: if a diversifier sleeve is already covered by a held
    # estate-safe substitute, top that up instead of opening the plan's ticker.
    topup: dict[str, str] = {}
    if holdings:
        from argosy.services.exposure_attribution import classify_plan_substitutes
        best: dict[str, float] = {}
        for s in classify_plan_substitutes(doc, holdings):
            if s.disposition == "topup" and s.held_value_usd > best.get(s.plan_instrument, 0.0):
                best[s.plan_instrument] = s.held_value_usd
                topup[s.plan_instrument] = s.held_ticker

    wsum = sum(w for _, w in divs) or float(len(divs))
    substitute_targets: set[str] = set()
    target_plan_sym: dict[str, str] = {}   # emitted target -> the plan sleeve instrument it fills
    add_by_sym: dict[str, float] = {}
    # Distribute with a running remainder so the shares sum EXACTLY to ``redirect``
    # (the last leg absorbs per-leg rounding) — no cents created or lost.
    divs_sorted = sorted(divs, key=lambda kv: (-kv[1], kv[0]))
    remaining = round(redirect, 2)
    for i, (sym, w) in enumerate(divs_sorted):
        share = remaining if i == len(divs_sorted) - 1 else min(
            round(redirect * (w / wsum), 2), remaining)
        remaining = round(remaining - share, 2)
        target = topup.get(sym, sym)
        if target != sym:
            substitute_targets.add(target)
        target_plan_sym.setdefault(target, sym)
        add_by_sym[target] = round(add_by_sym.get(target, 0.0) + share, 2)

    # Rebuild tiers: drop the blocked lines; increment an existing diversifier line
    # or synthesize a new core line for one not already proposed.
    existing_divs: set[str] = set()
    new_tiers = []
    for tier in plan.tiers:
        kept = []
        for line in tier.lines:
            sym = line.symbol.upper()
            if sym in drop:
                continue
            if sym in add_by_sym:
                kept.append(replace(line, amount_usd=round(line.amount_usd + add_by_sym[sym], 2),
                                    rationale=line.rationale + " [+redirected overflow from over-cap/funded-reserve lines]"))
                existing_divs.add(sym)
            else:
                kept.append(line)
        if kept:
            new_tiers.append(replace(tier, lines=tuple(kept)))

    to_add = [(s, a) for s, a in add_by_sym.items() if s not in existing_divs and a > 0.0]
    if to_add:
        synth = [
            DeploymentLine(
                symbol=sym, type="ETF", amount_usd=amt, timing="now",
                is_new=(sym not in substitute_targets),
                tier="core", horizon="10yr+",
                estate=EstateTag(domicile="IE", status="estate_safe",
                                 note="Irish UCITS diversifier (redirect target)"),
                cap_note="redirected overflow (over-cap/funded-reserve cash)",
                net_of_tax_caveat="",
                rationale=(
                    (f"Redirected overflow into {sym} — a zero-NVDA held diversifier "
                     "you already own — instead of buying over-cap NVDA exposure or "
                     "stacking a funded reserve (top up rather than open a new ticker).")
                    if sym in substitute_targets else
                    (f"Redirected overflow into {sym} — a zero-NVDA plan diversifier "
                     "— instead of buying over-cap NVDA exposure or stacking a funded "
                     "reserve. Reduces concentration.")
                ),
                cites=(
                    (f"plan_target:{target_plan_sym.get(sym, sym)}",)
                    + ((f"substitute:{sym}",) if sym in substitute_targets else ())
                ),
            )
            for sym, amt in to_add
        ]
        # Fold into a core tier (reuse the first core tier if present).
        merged = False
        for i, tier in enumerate(new_tiers):
            if tier.name == "core":
                new_tiers[i] = replace(tier, lines=tuple(list(tier.lines) + synth))
                merged = True
                break
        if not merged:
            from argosy.services.deployment_advisor import DeploymentTier
            new_tiers.append(DeploymentTier(name="core", cap_pct=0.0, lines=tuple(synth)))

    note = (
        f"Redirected ${redirect:,.0f} of over-cap / funded-reserve cash into your "
        f"plan's zero-NVDA diversifier ETFs ({', '.join(s for s, _ in divs)}) so the "
        f"full amount deploys into plan holdings (no gold / plan change needed)."
    )
    return replace(plan, tiers=tuple(new_tiers)), note


def rerank_plan(plan, sized):
    """Non-shadow: rebuild the DeploymentPlan so the buy list REFLECTS the
    verdict — vetoed/deferred/plan-change lines removed, capped lines resized to
    their final sized amount, freed cash pushed to the undeployed remainder (NOT
    force-redeployed). Estate exposure is recomputed from the surviving lines so
    the header numbers stay honest. Returns a new frozen DeploymentPlan."""
    from dataclasses import replace

    final_by_symbol = {sl.symbol.upper(): sl.final_usd for sl in sized.lines}

    new_tiers = []
    us_exposed = 0.0
    us_sanctioned = 0.0
    for tier in plan.tiers:
        kept = []
        for line in tier.lines:
            amt = final_by_symbol.get(line.symbol.upper())
            if amt is None or amt <= 0.0:
                continue
            kept.append(replace(line, amount_usd=amt))
            status = getattr(getattr(line, "estate", None), "status", "")
            if status == "us_situs_exposed":
                us_exposed += amt
            elif status == "us_situs_sanctioned":
                us_sanctioned += amt
        if kept:
            new_tiers.append(replace(tier, lines=tuple(kept)))

    deployed = round(sum(sl.final_usd for sl in sized.lines), 2)
    remainder = round(max(0.0, plan.deploy_amount_usd - deployed), 2)
    held_note = (
        f"Research check re-ranked this list: deploying ${deployed:,.0f} of "
        f"${plan.deploy_amount_usd:,.0f}; ${remainder:,.0f} held back (lines that "
        f"re-buy NVDA via look-through, stack the funded reserve, or couldn't be "
        f"price-verified). See the per-line reasons above."
    )
    return replace(
        plan,
        tiers=tuple(new_tiers),
        us_situs_exposed_usd=round(us_exposed, 2),
        us_situs_sanctioned_usd=round(us_sanctioned, 2),
        undeployed_remainder_usd=remainder,
        caveats=plan.caveats + (held_note,),
    )


__all__ = [
    "build_gate_inputs",
    "plan_to_candidates",
    "SnapshotOrLiveProvider",
    "run_preflight_for_plan",
    "rerank_plan",
]
