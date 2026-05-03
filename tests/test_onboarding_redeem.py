"""Phase 6: /api/onboarding/redeem flow."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient

from argosy.tenancy.database import reset_tenant_engines
from argosy.tenancy.onboarding import issue_setup_token, provision_tenant


@pytest.fixture(autouse=True)
def _isolate_argosy_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    reload_settings()
    yield
    import asyncio

    asyncio.run(reset_tenant_engines())


@pytest.mark.asyncio
async def test_redeem_happy_path(client: AsyncClient, engine: None) -> None:
    await provision_tenant("alice", email=None, plan="pro")
    token = await issue_setup_token("alice")

    r = await client.post(
        "/api/onboarding/redeem",
        json={"email": "alice@example.com", "token": token},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == "alice"
    assert body["plan"] == "pro"


@pytest.mark.asyncio
async def test_redeem_token_used_twice_fails(
    client: AsyncClient, engine: None
) -> None:
    await provision_tenant("alice", email=None, plan="free")
    token = await issue_setup_token("alice")

    r1 = await client.post(
        "/api/onboarding/redeem",
        json={"email": "alice@example.com", "token": token},
    )
    assert r1.status_code == 200

    r2 = await client.post(
        "/api/onboarding/redeem",
        json={"email": "alice@example.com", "token": token},
    )
    assert r2.status_code == 400


@pytest.mark.asyncio
async def test_redeem_email_mismatch_after_bind(
    client: AsyncClient, engine: None
) -> None:
    await provision_tenant("alice", email="alice@example.com", plan="free")
    token = await issue_setup_token("alice")

    r = await client.post(
        "/api/onboarding/redeem",
        json={"email": "mallory@example.com", "token": token},
    )
    # Don't reveal mismatch type — return 400 either way.
    assert r.status_code == 400
