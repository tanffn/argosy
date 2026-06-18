# argosy/quality/promote_gate.py
"""Single fail-closed promotion gate.

Draft 45 reached ``role='current'`` while codex said BLOCK, the deterministic gate said
FAIL, and the FM said rejected — because the deliberation reader (one axis) was allowed
to promote past the others. This gate makes promotion atomic across ALL authorities: a
plan becomes current ONLY when EVERY authority clears; a missing verdict is fail-closed;
no single authority can override the rest. On promote, the stale ``-fm-rejected`` slug is
relabeled and stale BLOCK receipts are stripped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Every authority that must clear before a plan can be promoted to current.
REQUIRED_AUTHORITIES = (
    "codex",                 # math/directive second opinion
    "deterministic_gate",    # in-stage invariant gate
    "fund_manager",          # FM decision
    "whole_artifact_reader", # prose-coherence reader / deliberation
    "rederivation",          # re-derive-blind reviewer (no inherited/tampered numbers)
)

_CLEAR_TOKENS = {
    "approve", "approve_with_conditions", "approved", "ok", "pass", "clear", "agree",
}


def _is_clear(verdict: object) -> bool:
    if isinstance(verdict, bool):
        return verdict
    return str(verdict).strip().lower() in _CLEAR_TOKENS


@dataclass
class PromoteDecision:
    can_promote: bool
    blocking_authorities: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def evaluate_promotion(
    authorities: dict[str, object], *, required: tuple[str, ...] = REQUIRED_AUTHORITIES,
) -> PromoteDecision:
    """Fail-closed across all authorities. ``authorities`` maps name -> verdict
    (e.g. "APPROVE"/"BLOCK"/"rejected" or a bool). Any missing or non-clear verdict
    blocks promotion."""
    blocking: list[str] = []
    reasons: list[str] = []
    for name in required:
        if name not in authorities:
            blocking.append(name)
            reasons.append(f"{name}: missing verdict -> fail-closed")
            continue
        if not _is_clear(authorities[name]):
            blocking.append(name)
            reasons.append(f"{name}: {authorities[name]!r}")
    return PromoteDecision(not blocking, blocking, reasons)


def relabel_on_promote(slug: str) -> str:
    """Strip a stale lifecycle suffix so an accepted plan can't read as fm-rejected."""
    return re.sub(r"-(fm-rejected|blocked|rejected)$", "", slug or "")
