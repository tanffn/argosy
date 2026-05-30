"""Phase 6: entitlements + decorator gating."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from argosy.billing.entitlements import (
    Entitlements,
    PlanTier,
    feature_required_tier,
)


def _write_yaml(tmp_path: Path, user_id: str, content: str) -> Path:
    cfg = tmp_path / user_id
    cfg.mkdir(parents=True, exist_ok=True)
    p = cfg / "entitlements.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_default_enterprise_plan_for_missing_yaml(tmp_path: Path) -> None:
    """Missing entitlements.yaml defaults to ENTERPRISE — matches the
    documented owner-operator single-tenant deployment shape (CLAUDE.md).
    Multi-tenant deployments must declare a plan explicitly per tenant.
    """
    ent = Entitlements.load("nobody", configs_dir=tmp_path)
    assert ent.plan is PlanTier.ENTERPRISE
    assert ent.has("agent_fleet_full") is True
    assert ent.has("autonomous_mode") is True
    assert ent.has("live_execution") is True
    assert math.isinf(ent.limit("monthly_decisions"))
    assert math.isinf(ent.limit("monthly_claude_spend_usd"))


def test_explicit_free_plan_still_gates(tmp_path: Path) -> None:
    """Multi-tenant case: an explicit `plan: free` YAML still locks the
    tenant out of pro/enterprise features. Only the missing-file default
    changed."""
    _write_yaml(tmp_path, "alice", "plan: free\n")
    ent = Entitlements.load("alice", configs_dir=tmp_path)
    assert ent.plan is PlanTier.FREE
    assert ent.has("autonomous_mode") is False
    assert ent.has("live_execution") is False
    assert ent.limit("monthly_decisions") == 50
    assert ent.limit("monthly_claude_spend_usd") == 5


def test_pro_plan_has_pro_features(tmp_path: Path) -> None:
    _write_yaml(tmp_path, "alice", "plan: pro\n")
    ent = Entitlements.load("alice", configs_dir=tmp_path)
    assert ent.plan is PlanTier.PRO
    assert ent.has("agent_fleet_full") is True
    assert ent.has("multi_account") is True
    assert ent.has("autonomous_mode") is False  # pro doesn't unlock autonomous


def test_enterprise_plan_unlocks_autonomous(tmp_path: Path) -> None:
    _write_yaml(tmp_path, "bob", "plan: enterprise\n")
    ent = Entitlements.load("bob", configs_dir=tmp_path)
    assert ent.plan is PlanTier.ENTERPRISE
    assert ent.has("autonomous_mode") is True
    assert ent.has("live_execution") is True
    assert math.isinf(ent.limit("monthly_decisions"))


def test_yaml_overrides_features(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path,
        "carol",
        """
plan: free
features:
  autonomous_mode: true
limits:
  monthly_decisions: 200
""",
    )
    ent = Entitlements.load("carol", configs_dir=tmp_path)
    assert ent.plan is PlanTier.FREE
    # Override flips a default-off feature on.
    assert ent.has("autonomous_mode") is True
    assert ent.limit("monthly_decisions") == 200


def test_unlimited_in_yaml(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path,
        "dave",
        """
plan: pro
limits:
  monthly_decisions: unlimited
""",
    )
    ent = Entitlements.load("dave", configs_dir=tmp_path)
    assert math.isinf(ent.limit("monthly_decisions"))


def test_feature_required_tier() -> None:
    assert feature_required_tier("autonomous_mode") is PlanTier.ENTERPRISE
    assert feature_required_tier("agent_fleet_full") is PlanTier.PRO
    # Unknown feature falls through to free (i.e., always allowed).
    assert feature_required_tier("not_a_feature") is PlanTier.FREE


@pytest.mark.asyncio
async def test_requires_feature_decorator_raises_402(monkeypatch, tmp_path: Path) -> None:
    """The decorator returns a 402 HTTPException when the feature is absent."""
    from fastapi import HTTPException

    from argosy.billing.decorators import requires_feature

    # Point the entitlements loader at our tmp configs dir.
    monkeypatch.setattr(
        "argosy.billing.entitlements.get_settings",
        lambda: type("S", (), {"configs_dir": tmp_path})(),
    )
    _write_yaml(tmp_path, "alice", "plan: free\n")

    @requires_feature("autonomous_mode")
    async def gated(body):  # noqa: ANN001 - test stub
        return "ok"

    class Body:
        user_id = "alice"

    with pytest.raises(HTTPException) as exc_info:
        await gated(body=Body())
    assert exc_info.value.status_code == 402
    assert exc_info.value.detail["feature"] == "autonomous_mode"


@pytest.mark.asyncio
async def test_requires_feature_passes_when_entitled(monkeypatch, tmp_path: Path) -> None:
    from argosy.billing.decorators import requires_feature

    monkeypatch.setattr(
        "argosy.billing.entitlements.get_settings",
        lambda: type("S", (), {"configs_dir": tmp_path})(),
    )
    _write_yaml(tmp_path, "bob", "plan: enterprise\n")

    @requires_feature("autonomous_mode")
    async def gated(body):  # noqa: ANN001
        return "ok"

    class Body:
        user_id = "bob"

    assert await gated(body=Body()) == "ok"
