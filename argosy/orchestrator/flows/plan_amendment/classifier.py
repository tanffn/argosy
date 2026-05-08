"""Classifier — turns an AmendmentIntent (advisor LLM output) into a
ClassificationResult (effective tier + delta + escalation reason).

Pure logic, no LLM call. The advisor's tier choice is honored unless:
  - tier="small" with direction in {"loosen", "ambiguous"} → escalate to medium
  - tier="small" without proposed_delta → escalate to medium (advisor failed to emit)

Escalation is one-way: small → medium. The user can manually re-ask for
large via a follow-up turn.
"""

from __future__ import annotations

from argosy.agents.advisor_amendment_types import AmendmentIntent
from argosy.orchestrator.flows.plan_amendment._types import (
    ClassificationResult,
    EffectiveTier,
)


def classify(intent: AmendmentIntent) -> ClassificationResult:
    """Map an advisor-emitted AmendmentIntent to its effective dispatch tier."""
    if intent.tier == "small":
        if intent.direction != "tighten":
            # Render None explicitly so escalation_reason stays a
            # readable string rather than embedding the literal "None".
            direction_str = intent.direction or "missing"
            return ClassificationResult(
                effective_tier=EffectiveTier.MEDIUM,
                proposed_delta=None,
                escalation_reason=f"small_with_{direction_str}_direction",
            )
        if intent.proposed_delta is None:
            return ClassificationResult(
                effective_tier=EffectiveTier.MEDIUM,
                proposed_delta=None,
                escalation_reason="small_without_delta",
            )
        return ClassificationResult(
            effective_tier=EffectiveTier.SMALL,
            proposed_delta=intent.proposed_delta,
            escalation_reason=None,
        )

    if intent.tier == "medium":
        return ClassificationResult(
            effective_tier=EffectiveTier.MEDIUM,
            proposed_delta=None,
            escalation_reason=None,
        )

    # tier == "large"
    return ClassificationResult(
        effective_tier=EffectiveTier.LARGE,
        proposed_delta=None,
        escalation_reason=None,
    )


__all__ = ["classify"]
