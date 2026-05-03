"""Settings API + cost-guard override route tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from argosy.api.routes.internal import _ensure_admin_token
from argosy.orchestrator.cost_guard import reset_cost_guard


@pytest.mark.asyncio
async def test_settings_get_returns_full_settings(engine: None, client: AsyncClient) -> None:
    res = await client.get("/api/settings", params={"user_id": "ariel"})
    assert res.status_code == 200
    body = res.json()
    # Required Phase 7 sections present.
    assert "cadences" in body
    assert "tiers" in body
    assert "models" in body
    assert "backups" in body
    assert "cost" in body
    assert "alerts" in body


@pytest.mark.asyncio
async def test_settings_patch_writes_back(engine: None, client: AsyncClient) -> None:
    res = await client.patch(
        "/api/settings",
        json={
            "user_id": "ariel",
            "patch": {
                "cost": {"monthly_budget_usd": 250.0},
                "alerts": {"email_to": "user@example.com"},
            },
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["cost"]["monthly_budget_usd"] == 250.0
    assert body["alerts"]["email_to"] == "user@example.com"


@pytest.mark.asyncio
async def test_settings_patch_validation_fails(engine: None, client: AsyncClient) -> None:
    res = await client.patch(
        "/api/settings",
        json={
            "user_id": "ariel",
            "patch": {"tiers": {"override_mode": ["not", "a", "string"]}},
        },
    )
    # pydantic should reject (override_mode is a string).
    assert res.status_code in (400, 422)


@pytest.mark.asyncio
async def test_cost_guard_override_requires_admin_token(
    engine: None, client: AsyncClient
) -> None:
    reset_cost_guard()
    res = await client.post(
        "/internal/cost-guard/override",
        json={"user_id": "ariel", "minutes": 30},
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_cost_guard_override_with_token(engine: None, client: AsyncClient) -> None:
    reset_cost_guard()
    token = _ensure_admin_token()
    res = await client.post(
        "/internal/cost-guard/override",
        json={"user_id": "ariel", "minutes": 30},
        headers={"X-Argosy-Admin-Token": token},
    )
    assert res.status_code == 200
    assert "expires_at" in res.json()
