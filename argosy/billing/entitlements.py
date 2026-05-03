"""Per-user entitlements loader.

Reads `${ARGOSY_HOME}/configs/<user_id>/entitlements.yaml`. The schema
is the one in SDD §12.2:

    plan: free | pro | enterprise
    features:
      agent_fleet_full: bool
      domain_kb_custom: bool
      multi_account: bool
      autonomous_mode: bool
      live_execution: bool
      api_access: bool
      telemetry_optout: bool
    limits:
      monthly_decisions: int | "unlimited"
      monthly_claude_spend_usd: float | "unlimited"

Missing fields fall back to the plan defaults below. This means a
tenant's YAML can be sparse ("plan: pro") and still get a sensible
entitlements bundle.
"""

from __future__ import annotations

import math
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from argosy.config import get_settings


class PlanTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"

    @classmethod
    def from_str(cls, value: str) -> "PlanTier":
        try:
            return cls(value.lower())
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError(f"unknown plan tier: {value!r}") from exc


# ----------------------------------------------------------------------
# Per-feature minimum tier. Drives `feature_required_tier()`.
# ----------------------------------------------------------------------

_FEATURE_MIN_TIER: dict[str, PlanTier] = {
    "agent_fleet_full": PlanTier.PRO,
    "domain_kb_custom": PlanTier.PRO,
    "multi_account": PlanTier.PRO,
    "autonomous_mode": PlanTier.ENTERPRISE,
    "live_execution": PlanTier.ENTERPRISE,
    "api_access": PlanTier.PRO,
    "telemetry_optout": PlanTier.PRO,
}


def feature_required_tier(feature: str) -> PlanTier:
    """Return the minimum tier required for `feature`."""
    return _FEATURE_MIN_TIER.get(feature, PlanTier.FREE)


# ----------------------------------------------------------------------
# Plan defaults
# ----------------------------------------------------------------------


def _default_features(plan: PlanTier) -> dict[str, bool]:
    """Derive the boolean feature flags for a plan tier."""
    out: dict[str, bool] = {}
    for feat, min_tier in _FEATURE_MIN_TIER.items():
        out[feat] = _tier_rank(plan) >= _tier_rank(min_tier)
    return out


def _tier_rank(tier: PlanTier) -> int:
    return {PlanTier.FREE: 0, PlanTier.PRO: 1, PlanTier.ENTERPRISE: 2}[tier]


def _default_limits(plan: PlanTier) -> dict[str, float]:
    if plan is PlanTier.FREE:
        return {"monthly_decisions": 50.0, "monthly_claude_spend_usd": 5.0}
    if plan is PlanTier.PRO:
        return {"monthly_decisions": 1000.0, "monthly_claude_spend_usd": 100.0}
    return {
        "monthly_decisions": math.inf,
        "monthly_claude_spend_usd": math.inf,
    }


# ----------------------------------------------------------------------
# Pydantic model
# ----------------------------------------------------------------------


class Entitlements(BaseModel):
    """Resolved entitlements for one tenant."""

    user_id: str
    plan: PlanTier = PlanTier.FREE
    features: dict[str, bool] = Field(default_factory=dict)
    limits: dict[str, float] = Field(default_factory=dict)

    @field_validator("plan", mode="before")
    @classmethod
    def _coerce_plan(cls, v: Any) -> PlanTier:
        if isinstance(v, PlanTier):
            return v
        return PlanTier.from_str(str(v))

    def has(self, feature: str) -> bool:
        """True if this tenant has `feature` enabled."""
        return bool(self.features.get(feature, False))

    def limit(self, name: str) -> float:
        """Numeric limit, with `inf` for unlimited / unset."""
        return float(self.limits.get(name, math.inf))

    @classmethod
    def load(cls, user_id: str, *, configs_dir: Path | None = None) -> "Entitlements":
        """Load `entitlements.yaml` for a user; fall back to plan defaults."""
        cfg_dir = configs_dir or get_settings().configs_dir
        path = cfg_dir / user_id / "entitlements.yaml"
        plan = PlanTier.FREE
        features: dict[str, bool] | None = None
        limits: dict[str, Any] | None = None
        if path.is_file():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:  # pragma: no cover - defensive
                data = {}
            if isinstance(data, dict):
                plan = PlanTier.from_str(str(data.get("plan", "free")))
                features = (
                    data.get("features") if isinstance(data.get("features"), dict) else None
                )
                limits = data.get("limits") if isinstance(data.get("limits"), dict) else None

        merged_features = _default_features(plan)
        if features:
            for k, v in features.items():
                merged_features[k] = bool(v)

        merged_limits = _default_limits(plan)
        if limits:
            for k, v in limits.items():
                merged_limits[k] = _coerce_limit(v)

        return cls(
            user_id=user_id,
            plan=plan,
            features=merged_features,
            limits=merged_limits,
        )


def _coerce_limit(v: Any) -> float:
    if isinstance(v, str):
        if v.lower() in ("unlimited", "infinity", "inf"):
            return math.inf
        try:
            return float(v)
        except ValueError:
            return math.inf
    if v is None:
        return math.inf
    return float(v)


__all__ = ["Entitlements", "PlanTier", "feature_required_tier"]
