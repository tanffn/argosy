"""Plan-aware windfall allocator — reads the TSV's Current Allocation table
and generates ranked allocation proposals split across three horizons.

User spec (2026-05-28):
  - LONG-TERM: closes the largest negative deltas vs plan targets
                 (deterministic, fast, low-confidence sensitivity).
  - MEDIUM-TERM: 3-12mo thesis trades — handed off to the multi-agent
                 fleet for full debate (analysts → bull/bear → trader →
                 risk → FM). Lives in a follow-up commit.
  - SHORT-TERM: opportunistic entries from the watchlist + recent news.
                 Also fleet-driven.

This module implements the LONG-TERM cut today. The medium + short
horizon outputs are stubbed with rationale pointing to the fleet
integration that follows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from argosy.services.retirement.citations import ValueWithRationale
from argosy.services.retirement.windfall_detector import (
    AllocationLine,
    WindfallEvent,
)


Horizon = Literal["long", "medium", "short"]


# Map asset_class names from the TSV to preferred instruments. Picks
# instruments the user already holds when possible (per user spec: "prefer
# what's in your portfolio"). Falls back to a sensible default if none.
# DOMICILE-AWARE (S18): UCITS (Irish-domiciled) twins, NOT US-domiciled ETFs —
# for a non-US-person US-domiciled shares are US-situs and rebuild the estate-tax
# tail (cite estate_tax_nonresidents.md / feedback_canonical_allocation_ucits_preferred).
# This mirrors the canonical allocation_plan engine. NOTE: this hardcoded map is
# the legacy class-level path; the proper fix is to bind windfall buys to the
# canonical instrument-level TargetAllocationDoc (diff_plan_vs_holdings) — tracked.
_PREFERRED_INSTRUMENTS_BY_CLASS: dict[str, list[str]] = {
    "Core Equity":       ["CSPX", "ACWD"],
    "Defensive":         ["IB01", "IBTA"],
    "Dividend":          ["FUSA"],
    "Growth":            ["CNDX", "R1GR"],
    "Individual Stocks": ["SMGB", "WTAI"],  # high-potential via UCITS thematic, non-US-situs
    "International":     ["EXUS", "FWRA"],
    "Alternative":       ["DPYA"],
}


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


def _under_target(line: AllocationLine) -> bool:
    """Under target = positive delta.

    TSV convention: ``delta = target - current``. So:
      - positive delta  → current < target → UNDER target (need to add)
      - negative delta  → current > target → OVER target  (need to trim)
    Cash is excluded from "under target" candidates regardless of sign —
    a windfall is cash, allocating cash to cash is a no-op.
    """
    if line.asset_class.strip().lower() == "cash":
        return False
    return line.delta_k_usd > 0


def _allocate_long_term(
    windfall_usd: float,
    allocation_table: list[AllocationLine],
    *,
    long_term_budget_fraction: float = 0.60,
) -> tuple[list[AllocationProposal], float]:
    """Plan-target closing allocation.

    Allocates 60% of the windfall to closing the largest negative deltas
    (under-target asset classes). Within each class, picks the user's
    preferred instrument.
    """
    proposals: list[AllocationProposal] = []
    budget_usd = windfall_usd * long_term_budget_fraction
    remaining = budget_usd

    # Take only under-target classes, sorted by magnitude of gap descending
    under = sorted(
        (line for line in allocation_table if _under_target(line)),
        key=lambda line: -line.delta_k_usd,  # largest positive delta first
    )

    for line in under:
        if remaining <= 0:
            break
        gap_usd = abs(line.delta_k_usd) * 1000.0
        allocate = min(remaining, gap_usd)
        if allocate <= 0:
            continue
        instruments = _PREFERRED_INSTRUMENTS_BY_CLASS.get(
            line.asset_class, [],
        )
        # Spread across up to 2 instruments per class for diversification
        if len(instruments) >= 2 and allocate >= 30_000:
            split = [allocate * 0.6, allocate * 0.4]
            picks = instruments[:2]
        elif instruments:
            split = [allocate]
            picks = [instruments[0]]
        else:
            split = [allocate]
            picks = ["(class-level; pick ticker)"]
        for amount, instr in zip(split, picks):
            proposals.append(AllocationProposal(
                horizon="long",
                asset_class=line.asset_class,
                instrument=instr,
                amount_usd=amount,
                rationale=(
                    f"Closes the {line.asset_class} plan-target gap "
                    f"(${abs(line.delta_k_usd):,.0f}K under target). "
                    f"Preferred instrument: {instr} (already in your portfolio)."
                    if instr != "(class-level; pick ticker)"
                    else f"Closes {line.asset_class} gap; no preferred "
                         "instrument registered — pick a ticker in this class."
                ),
                closes_delta_usd=amount,
                confidence="high",
                source_id="argosy_derived",
            ))
        remaining -= allocate

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
    long_term_budget_fraction: float = 0.60,
    medium_term_budget_fraction: float = 0.25,
    short_term_budget_fraction: float = 0.15,
) -> WindfallAllocationPlan:
    """Generate ranked long/medium/short allocation proposals."""
    windfall_usd = max(0.0, windfall.cash_delta_total_usd_equiv)

    long_term, remaining_after_long = _allocate_long_term(
        windfall_usd,
        windfall.allocation_delta_table,
        long_term_budget_fraction=long_term_budget_fraction,
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
