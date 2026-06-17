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


def build_value_resolution(
    dispute: "Dispute", *, canonical_text: str, sites: list[tuple[str, str]],
    stale_text: str = "",
) -> dict:
    """Build a deterministic patch+invariant set conforming every site to the
    canonical value. `sites` is [(surface_id, conform_method)]. For markdown sites a
    stale_text->canonical_text replacement is emitted (no-op if stale_text empty).
    The invariant asserts equals_canonical across all sites."""
    patches: list[dict] = []
    for surface_id, method in sites:
        if method == "markdown" and stale_text:
            patches.append({
                "surface_id": surface_id, "conform_method": "markdown",
                "find": stale_text, "replace": canonical_text,
            })
    invariant = [{
        "kind": "equals_canonical",
        "subject_type": dispute.subject_type,
        "canonical_text": canonical_text,
        "surfaces": tuple(s for s, _ in sites),
    }]
    return {"patches": patches, "invariant": invariant}
