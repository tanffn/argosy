"""Publish gate for the living plan (Layer 3).

A plan is promotable ONLY when (1) every promote_gate authority clears AND
(2) no node carries an open HARD or COHERENCE status_flag. Hash-validity
alone never authorizes publication. This wraps the existing
argosy/quality/promote_gate.py::evaluate_promotion authority set and folds the
open-flag check in front of it, fail-closed.

Registered as a STANDALONE wrapper over promote_gate.evaluate_promotion; the
live /accept + orchestrator promotion call sites are deliberately NOT rerouted
through this gate here (that wiring is a follow-on).
"""
from __future__ import annotations

from dataclasses import dataclass

from argosy.quality.promote_gate import (
    PromoteDecision, REQUIRED_AUTHORITIES, evaluate_promotion,
)

# Flag kinds that block publication. Anything else (info/cosmetic) does not.
_BLOCKING_FLAG_KINDS = {"hard", "coherence"}


@dataclass
class OpenFlag:
    node_key: str
    kind: str  # "hard" | "coherence" | "info" | ...


def can_publish_plan(
    *,
    authorities: dict[str, object],
    open_flags: list[OpenFlag],
    required: tuple[str, ...] = REQUIRED_AUTHORITIES,
) -> PromoteDecision:
    """Fail-closed publish decision. An open hard/coherence flag blocks even
    when every authority clears; a missing authority blocks via promote_gate."""
    base = evaluate_promotion(authorities, required=required)
    blocking = list(base.blocking_authorities)
    reasons = list(base.reasons)
    for flag in open_flags:
        if flag.kind in _BLOCKING_FLAG_KINDS:
            tag = f"open-{flag.kind}-flag:{flag.node_key}"
            blocking.append(tag)
            reasons.append(f"{tag}: node carries an open {flag.kind} flag -> fail-closed")
    return PromoteDecision(not blocking, blocking, reasons)


__all__ = ["OpenFlag", "can_publish_plan"]
