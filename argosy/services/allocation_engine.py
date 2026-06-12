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


__all__ = [
    "AllocationMode", "AllocationLeg", "AllocationCandidate", "REPLACES_SYMBOLS",
]
