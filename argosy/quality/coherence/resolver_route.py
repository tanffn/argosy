# argosy/quality/coherence/resolver_route.py
"""Deterministic routing: value/calc mismatches with a canonical source go to the
resolver; representation mismatches with a canonical render go to the resolver too;
policy/framing tensions go to arbitration; anything un-typeable BLOCKs."""
from __future__ import annotations

import enum

from argosy.quality.coherence.dispute import Dispute


class RouteKind(enum.Enum):
    RESOLVER = "resolver"
    ARBITRATION = "arbitration"
    BLOCK = "block"


def route_dispute(d: Dispute) -> RouteKind:
    if not d.subject_type:
        return RouteKind.BLOCK
    if d.conflict_type in ("value_mismatch", "calc_inconsistency", "representation_mismatch"):
        return RouteKind.RESOLVER
    if d.conflict_type == "policy_tension":
        return RouteKind.ARBITRATION
    return RouteKind.BLOCK
