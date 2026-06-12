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


__all__ = [
    "AllocationMode", "AllocationLeg", "AllocationCandidate", "REPLACES_SYMBOLS",
    "class_targets_as_of",
]
