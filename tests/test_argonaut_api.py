"""Argonaut + security API route tests (Phase 5)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from argosy.security import totp as totp_mod
from argosy.state import db as db_mod
from argosy.state.models import User


async def _seed_user() -> None:
    async with db_mod.get_session() as session:
        if await session.get(User, "ariel") is None:
            session.add(User(id="ariel"))
            await session.commit()


@pytest.mark.asyncio
async def test_argonaut_status_endpoint(client: AsyncClient) -> None:
    await _seed_user()
    r = await client.get("/api/argonaut/status?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "ariel"
    assert "execution_mode" in body
    assert "autonomy_enabled" in body
    assert "open_positions" in body


@pytest.mark.asyncio
async def test_argonaut_snapshot_endpoint(client: AsyncClient) -> None:
    await _seed_user()
    r = await client.post(
        "/api/argonaut/snapshot",
        json={"user_id": "ariel", "mode": "paper"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "date" in body
    assert "total_value_usd" in body


@pytest.mark.asyncio
async def test_argonaut_snapshots_list(client: AsyncClient) -> None:
    await _seed_user()
    # Force one
    r = await client.post(
        "/api/argonaut/snapshot", json={"user_id": "ariel", "mode": "paper"}
    )
    assert r.status_code == 200
    r2 = await client.get("/api/argonaut/snapshots?user_id=ariel")
    assert r2.status_code == 200
    rows = r2.json()["rows"]
    assert len(rows) >= 1


@pytest.mark.asyncio
async def test_argonaut_mode_toggle(client: AsyncClient, tmp_path) -> None:
    await _seed_user()
    r = await client.post(
        "/api/argonaut/mode", json={"user_id": "ariel", "mode": "queue_only"}
    )
    assert r.status_code == 200
    assert r.json()["mode"] == "queue_only"
    # Status reflects the change
    r2 = await client.get("/api/argonaut/status?user_id=ariel")
    assert r2.json()["execution_mode"] == "queue_only"
    assert r2.json()["autonomy_enabled"] is False
    # Reset to paper
    await client.post(
        "/api/argonaut/mode", json={"user_id": "ariel", "mode": "paper"}
    )


@pytest.mark.asyncio
async def test_totp_setup_and_verify(client: AsyncClient) -> None:
    await _seed_user()
    r = await client.post(
        "/api/security/totp/setup", json={"user_id": "ariel"}
    )
    assert r.status_code == 200
    body = r.json()
    secret = body["secret"]
    assert body["provisioning_uri"].startswith("otpauth://")

    # Generate a valid code locally and verify via API.
    code = totp_mod.generate_code(secret)
    r2 = await client.post(
        "/api/security/totp/verify",
        json={"user_id": "ariel", "code": code},
    )
    assert r2.status_code == 200
    assert r2.json()["ok"] is True


@pytest.mark.asyncio
async def test_totp_verify_rejects_bad_code(client: AsyncClient) -> None:
    await _seed_user()
    await client.post("/api/security/totp/setup", json={"user_id": "ariel"})
    r = await client.post(
        "/api/security/totp/verify", json={"user_id": "ariel", "code": "000000"}
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False


@pytest.mark.asyncio
async def test_totp_status_before_and_after_setup(client: AsyncClient) -> None:
    await _seed_user()
    r0 = await client.get("/api/security/totp/status?user_id=ariel")
    assert r0.status_code == 200
    assert r0.json()["enrolled"] is False
    await client.post("/api/security/totp/setup", json={"user_id": "ariel"})
    r1 = await client.get("/api/security/totp/status?user_id=ariel")
    assert r1.json()["enrolled"] is True
