"""Phase 6: /internal/* admin routes."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from argosy.api.routes.internal import _ensure_admin_token
from argosy.state import db as db_mod
from argosy.state.models import User


@pytest.mark.asyncio
async def test_internal_health_full_requires_token(client: AsyncClient) -> None:
    r = await client.get("/internal/health/full?user_id=ariel")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_internal_health_full_with_token(client: AsyncClient) -> None:
    async with db_mod.get_session() as s:
        s.add(User(id="ariel"))
        await s.commit()

    token = _ensure_admin_token()
    r = await client.get(
        "/internal/health/full?user_id=ariel",
        headers={"X-Argosy-Admin-Token": token},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "ariel"
    assert "breaches" in body
    assert "claude_monthly_spend_usd" in body


@pytest.mark.asyncio
async def test_internal_telemetry_receiver(client: AsyncClient) -> None:
    token = _ensure_admin_token()
    r = await client.post(
        "/internal/telemetry",
        headers={"X-Argosy-Admin-Token": token},
        json={
            "bucket": "usage",
            "name": "decision_run",
            "fields": {"tier": "T1"},
            "timestamp": 0.0,
            "user_hash": "abc123",
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_internal_telemetry_requires_token(client: AsyncClient) -> None:
    r = await client.post(
        "/internal/telemetry",
        json={"bucket": "usage", "name": "x"},
    )
    assert r.status_code == 401
