"""Internal types for plan_amendment flow (Wave 4).

Public types live in argosy.agents.advisor_amendment_types. These are
the post-classification effective values used internally by the
dispatcher + workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from argosy.agents.plan_synthesizer_types import Delta


class EffectiveTier(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


@dataclass(frozen=True)
class ClassificationResult:
    """Output of the classifier — what the dispatcher should actually do."""

    effective_tier: EffectiveTier
    proposed_delta: Delta | None
    escalation_reason: str | None  # set when effective_tier != requested tier


__all__ = ["EffectiveTier", "ClassificationResult"]
