"""Phase 6: telemetry client tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from argosy.telemetry.client import (
    TelemetryClient,
    TelemetryConfig,
    reset_client,
)


@pytest.mark.asyncio
async def test_disabled_client_is_noop() -> None:
    client = TelemetryClient(config=TelemetryConfig(enabled=False))
    event = await client.record("usage", "decision_run", user_id="alice")
    assert event is None
    assert client.sent == []


@pytest.mark.asyncio
async def test_enabled_client_sends_via_transport() -> None:
    captured: list[dict[str, Any]] = []

    async def transport(endpoint: str, payload: dict[str, Any]) -> None:
        captured.append({"endpoint": endpoint, "payload": payload})

    client = TelemetryClient(
        config=TelemetryConfig(
            enabled=True,
            endpoint="https://telemetry.example.com/v1",
            salt="test-salt",
        ),
        transport=transport,
    )

    event = await client.record(
        "usage",
        "decision_run",
        user_id="alice",
        fields={"tier": "T2", "count": 1},
    )
    assert event is not None
    # Wait for the fire-and-forget task.
    for _ in range(20):
        if captured:
            break
        await asyncio.sleep(0.01)
    assert len(captured) == 1
    payload = captured[0]["payload"]
    assert payload["bucket"] == "usage"
    assert payload["name"] == "decision_run"
    assert payload["fields"]["tier"] == "T2"
    # Identity is anonymized.
    assert payload["user_hash"] != "alice"
    assert len(payload["user_hash"]) == 16


@pytest.mark.asyncio
async def test_redacts_forbidden_fields() -> None:
    client = TelemetryClient(
        config=TelemetryConfig(enabled=True, endpoint="x"),
        transport=lambda *a, **k: asyncio.sleep(0),
    )
    event = await client.record(
        "usage",
        "test",
        user_id="alice",
        fields={
            "ticker": "NVDA",
            "value_usd": 5000,
            "plan_content": "secret plan text",
            "email": "alice@example.com",
            "tier": "T2",  # allowed
            "count": 5,    # allowed
        },
    )
    assert event is not None
    assert "ticker" not in event.fields
    assert "value_usd" not in event.fields
    assert "plan_content" not in event.fields
    assert "email" not in event.fields
    assert event.fields == {"tier": "T2", "count": 5}


@pytest.mark.asyncio
async def test_user_hash_stable_per_salt() -> None:
    c = TelemetryClient(config=TelemetryConfig(enabled=True, salt="s1"))
    h1 = c.hash_user("alice")
    h2 = c.hash_user("alice")
    h3 = c.hash_user("bob")
    assert h1 == h2 != h3


@pytest.mark.asyncio
async def test_unknown_bucket_dropped() -> None:
    client = TelemetryClient(config=TelemetryConfig(enabled=True))
    event = await client.record("nonsense", "x", user_id="alice")  # type: ignore[arg-type]
    assert event is None


def test_reset_client_returns_singleton() -> None:
    fresh = TelemetryClient(config=TelemetryConfig(enabled=True))
    out = reset_client(fresh)
    assert out is fresh
