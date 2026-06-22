"""Plan-aware windfall allocator — generates ranked allocation proposals split
across three horizons.

User spec (2026-05-28):
  - LONG-TERM: closes the largest gaps vs the CANONICAL plan targets
                 (deterministic, fast, low-confidence sensitivity).
  - MEDIUM-TERM: 3-12mo thesis trades — handed off to the multi-agent
                 fleet for full debate (analysts → bull/bear → trader →
                 risk → FM). Lives in a follow-up commit.
  - SHORT-TERM: opportunistic entries from the watchlist + recent news.
                 Also fleet-driven.

The LONG-TERM cut is bound to the canonical instrument-level
``TargetAllocationDoc`` via :func:`propose_allocations_from_plan`, which
delegates to ``allocation_engine.cash_only_deploy`` — the SAME path the
``/deploy-cash`` surface uses. There is exactly ONE buy list across the
windfall, unallocated-cash, and deploy-cash surfaces: feed the same
``(doc, holdings, cash)`` and you get the same instruments. The medium +
short horizon outputs are stubbed with rationale pointing to the fleet
integration that follows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from argosy.services.retirement.citations import ValueWithRationale
from argosy.services.retirement.windfall_detector import WindfallEvent


Horizon = Literal["long", "medium", "short"]


@dataclass
class AllocationProposal:
    horizon: Horizon
    asset_class: str
    instrument: str
    amount_usd: float
    rationale: str
    closes_delta_usd: float
    confidence: Literal["high", "medium", "low"] = "medium"
    source_id: str = "argosy_derived"

    def to_dict(self) -> dict:
        return {
            "horizon": self.horizon,
            "asset_class": self.asset_class,
            "instrument": self.instrument,
            "amount_usd": round(self.amount_usd, 2),
            "rationale": self.rationale,
            "closes_delta_usd": round(self.closes_delta_usd, 2),
            "confidence": self.confidence,
            "source_id": self.source_id,
        }


@dataclass
class WindfallAllocationPlan:
    windfall_usd: float
    long_term: list[AllocationProposal]
    medium_term: list[AllocationProposal]
    short_term: list[AllocationProposal]
    remaining_unallocated_usd: float
    headline: ValueWithRationale  # one-line summary

    def to_dict(self) -> dict:
        return {
            "windfall_usd": round(self.windfall_usd, 2),
            "long_term": [p.to_dict() for p in self.long_term],
            "medium_term": [p.to_dict() for p in self.medium_term],
            "short_term": [p.to_dict() for p in self.short_term],
            "remaining_unallocated_usd": round(self.remaining_unallocated_usd, 2),
            "headline": {
                "value": self.headline.value,
                "unit": self.headline.unit,
                "rationale": self.headline.rationale,
                "source_id": self.headline.source_id,
            },
        }


def _symbol_to_class_label(doc) -> dict[str, str]:
    """symbol -> canonical class label, for annotating buy proposals with the
    plan class a buy fills. A symbol appearing in >1 class keeps the first."""
    out: dict[str, str] = {}
    for cls in getattr(doc, "classes", []) or []:
        for instr in cls.instruments:
            out.setdefault(instr.symbol, cls.label)
    return out


def _candidates_to_long_proposals(
    candidates, doc,
) -> list[AllocationProposal]:
    """Map canonical ``AllocationCandidate[]`` (from ``cash_only_deploy``) onto the
    long-term ``AllocationProposal`` DTO the UI surfaces consume.

    Every BUY leg becomes one ``horizon='long'`` proposal whose ``instrument`` is
    the canonical plan symbol (NOT a hardcoded class default) and whose
    ``asset_class`` is the canonical class the symbol fills. ``closes_delta_usd``
    equals the deployed amount — the engine sizes each buy at its plan-target gap
    (water-filled to cash), so the deployed dollars ARE the gap closed.
    """
    sym_to_class = _symbol_to_class_label(doc)
    proposals: list[AllocationProposal] = []
    for cand in candidates:
        for leg in cand.legs:
            if leg.side != "BUY":
                continue
            amount = round(abs(leg.notional_usd), 2)
            if amount <= 0:
                continue
            label = sym_to_class.get(leg.symbol, "(plan instrument)")
            proposals.append(AllocationProposal(
                horizon="long",
                asset_class=label,
                instrument=leg.symbol,
                amount_usd=amount,
                rationale=cand.rationale or (
                    f"Deploy ${amount:,.0f} into {leg.symbol} toward its "
                    f"canonical plan target ({label})."
                ),
                closes_delta_usd=amount,
                confidence="high",
                source_id="argosy_derived",
            ))
    return proposals


def _allocate_long_term_from_plan(
    budget_usd: float,
    doc,
    holdings: dict[str, float],
    *,
    as_of: date,
) -> tuple[list[AllocationProposal], float]:
    """Canonical plan-bound long-term allocation.

    Deploys ``budget_usd`` toward the glide-aware instrument-level targets via
    the SAME ``cash_only_deploy`` engine ``/deploy-cash`` uses, then maps the
    resulting candidates into the proposal DTO. Returns ``(proposals, remaining)``
    where ``remaining`` is the budget the engine could not place against current
    plan targets (surfaced, never silently dropped).
    """
    if doc is None:
        raise ValueError(
            "windfall long-term allocation requires the canonical "
            "TargetAllocationDoc — refusing to fall back to a hardcoded "
            "instrument map (accept a plan first).")
    if budget_usd <= 0:
        return [], max(0.0, budget_usd)
    candidates = propose_allocations_from_plan(
        doc, holdings, budget_usd, as_of=as_of)
    proposals = _candidates_to_long_proposals(candidates, doc)
    placed = sum(p.amount_usd for p in proposals)
    remaining = round(max(0.0, budget_usd - placed), 2)
    return proposals, remaining


def _stub_medium_term(
    windfall_usd: float,
    *,
    medium_term_budget_fraction: float = 0.25,
) -> list[AllocationProposal]:
    """Stub for the medium-term proposals.

    The user's spec is: hand the medium-term budget to the multi-agent
    fleet (analysts → bull/bear → trader → risk → FM) to debate 3-12mo
    thesis trades. That integration ships in the next commit; here we
    emit a single placeholder proposal pointing at the fleet entry.
    """
    budget = windfall_usd * medium_term_budget_fraction
    if budget <= 0:
        return []
    return [AllocationProposal(
        horizon="medium",
        asset_class="(agent-fleet-driven)",
        instrument="<await fleet synthesis>",
        amount_usd=budget,
        rationale=(
            f"Medium-term budget (${budget:,.0f}) — agent fleet runs a "
            "5-phase synthesis (analysts → bull/bear debate → trader → "
            "3 risk officers → fund manager) to propose 3-12mo thesis "
            "trades. Output: 2-4 specific tickers with target prices + "
            "catalysts. ETA: ~30-70 min per Argosy synthesis stack."
        ),
        closes_delta_usd=0,
        confidence="low",
        source_id="argosy_derived",
    )]


def _stub_short_term(
    windfall_usd: float,
    *,
    short_term_budget_fraction: float = 0.15,
) -> list[AllocationProposal]:
    """Stub for the short-term proposals.

    Opportunistic entries from the watchlist + recent news. Also fleet-
    driven; placeholder for now.
    """
    budget = windfall_usd * short_term_budget_fraction
    if budget <= 0:
        return []
    return [AllocationProposal(
        horizon="short",
        asset_class="(watchlist + news)",
        instrument="<await opportunity scan>",
        amount_usd=budget,
        rationale=(
            f"Short-term budget (${budget:,.0f}) — opportunistic entries "
            "from the watchlist + recent investor-event signals. If no "
            "high-conviction entries, parks in IB01 (~4% yield, Irish UCITS "
            "$-Treasury — non-US-situs) or a direct T-bill for 1-3 months "
            "until something surfaces."
        ),
        closes_delta_usd=0,
        confidence="low",
        source_id="argosy_derived",
    )]


def propose_allocations(
    windfall: WindfallEvent,
    *,
    doc,
    holdings: dict[str, float],
    as_of: date,
    long_term_budget_fraction: float = 0.60,
    medium_term_budget_fraction: float = 0.25,
    short_term_budget_fraction: float = 0.15,
) -> WindfallAllocationPlan:
    """Generate ranked long/medium/short allocation proposals.

    The horizon BUDGET split (60/25/15) is preserved for the amount math, but
    the long-term INSTRUMENTS come from the canonical ``TargetAllocationDoc``
    (via ``cash_only_deploy``), so the same cash yields the same instruments as
    ``/deploy-cash``. ``doc`` is required — fail loud rather than fall back to a
    hardcoded class→instrument map.
    """
    windfall_usd = max(0.0, windfall.cash_delta_total_usd_equiv)

    long_term, _remaining_after_long = _allocate_long_term_from_plan(
        windfall_usd * long_term_budget_fraction,
        doc, holdings, as_of=as_of,
    )

    medium_term = _stub_medium_term(
        windfall_usd,
        medium_term_budget_fraction=medium_term_budget_fraction,
    )
    short_term = _stub_short_term(
        windfall_usd,
        short_term_budget_fraction=short_term_budget_fraction,
    )

    total_allocated = sum(p.amount_usd for p in long_term + medium_term + short_term)
    remaining = max(0.0, windfall_usd - total_allocated)

    headline = ValueWithRationale(
        value=f"${windfall_usd:,.0f}",
        unit="USD",
        source_id="argosy_derived",
        rationale=(
            f"{windfall.classified_source} of ${windfall_usd:,.0f} detected. "
            f"Long-term: {len(long_term)} proposals closing plan-target gaps. "
            f"Medium-term: {len(medium_term)} agent-fleet placeholder. "
            f"Short-term: {len(short_term)} opportunistic placeholder."
        ),
        confidence="medium" if not windfall.requires_user_classification else "low",
    )

    return WindfallAllocationPlan(
        windfall_usd=windfall_usd,
        long_term=long_term,
        medium_term=medium_term,
        short_term=short_term,
        remaining_unallocated_usd=remaining,
        headline=headline,
    )


def propose_allocations_from_plan(doc, holdings, cash_usd, *, as_of):
    """Plan-bound cash deployment — THE canonical long-term buy list. Delegates to
    the deterministic allocation engine (glide-aware targets, buy-only and
    cash-constrained), so 'plan target' is the canonical instrument-level
    TargetAllocationDoc. This is the SAME engine ``/deploy-cash`` calls; the
    windfall + unallocated-cash long-term proposals route through it (via
    :func:`_allocate_long_term_from_plan`) so all three surfaces agree.

    Returns ``AllocationCandidate[]`` (the cross-phase contract)."""
    from argosy.services.allocation_engine import AllocationMode, compute_allocation

    return compute_allocation(doc, holdings, AllocationMode.CASH_ONLY_DEPLOY,
                              cash_usd=cash_usd, as_of=as_of)
