"""Argosy billing / entitlements (Phase 6).

Phase 1 stubbed `entitlements.yaml` with full access; Phase 6 makes it
real. Every gated feature checks `entitlements.has(feature)`. Adding
billing later means swapping `Entitlements.load()` from a YAML loader
to a Stripe webhook receiver — the call sites don't change.
"""

from __future__ import annotations

from argosy.billing.decorators import (
    EntitlementError,
    QuotaExceededError,
    requires_feature,
    requires_within_quota,
)
from argosy.billing.entitlements import (
    Entitlements,
    PlanTier,
    feature_required_tier,
)

__all__ = [
    "Entitlements",
    "EntitlementError",
    "PlanTier",
    "QuotaExceededError",
    "feature_required_tier",
    "requires_feature",
    "requires_within_quota",
]
