from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from argosy.api.routes.health import router
from argosy.services.chat_advisor.connection_status import connection_status
from argosy.transport.discord_advisor import config as config_module


def configured(monkeypatch, *, enabled=True):
    value = SimpleNamespace(enabled=enabled, bindings=[SimpleNamespace(household_user_id="owner")])
    monkeypatch.setattr(config_module, "load_config", lambda: value)


def test_unconfigured_is_not_red(monkeypatch):
    configured(monkeypatch, enabled=False)
    value = connection_status(SimpleNamespace(), user_id="owner")
    assert value["status"] == "unconfigured"
    assert not value["attention_required"]


def test_does_not_expose_other_household_connection(monkeypatch):
    configured(monkeypatch)
    value = connection_status(SimpleNamespace(), user_id="foreign")
    assert value["status"] == "unconfigured"
    assert not value["attention_required"]


def test_enabled_but_not_running_is_actionable(monkeypatch):
    configured(monkeypatch)
    value = connection_status(SimpleNamespace(), user_id="owner")
    assert value["attention_required"]
    assert "Restart" in value["message"]


@pytest.mark.asyncio
async def test_health_route_projects_live_worker_state_without_private_ids(monkeypatch):
    configured(monkeypatch)
    app = FastAPI()
    app.include_router(router)
    app.state.discord_advisor_job = SimpleNamespace(status_snapshot=lambda: {
        "connection": "connected", "error": None, "guild_id": "private-id-not-for-response",
    })
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/discord-advisor", params={"user_id": "owner"})
    assert response.status_code == 200
    assert response.json()["status"] == "connected"
    assert "private-id" not in response.text


def test_auth_failure_is_visible_and_credentials_redacted(monkeypatch):
    configured(monkeypatch)
    job = SimpleNamespace(status_snapshot=lambda: {
        "connection": "stopped", "error": "Authentication rejected token=do-not-expose-me",
        "repair": "Replace the token locally and restart.",
    })
    value = connection_status(SimpleNamespace(discord_advisor_job=job), user_id="owner")
    assert value["attention_required"]
    assert "do-not-expose-me" not in value["message"]
    assert "Replace" in value["message"]


def test_status_board_failure_does_not_claim_chat_disconnected(monkeypatch):
    configured(monkeypatch)
    job = SimpleNamespace(status_snapshot=lambda: {
        "connection": "connected", "error": None,
        "status_board_error": "Scheduled-job board could not refresh; chat remains available.",
    })
    value = connection_status(SimpleNamespace(discord_advisor_job=job), user_id="owner")
    assert value["status"] == "connected"
    assert "board could not refresh" in value["message"]
