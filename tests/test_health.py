"""GET /health smoke test."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from argosy import __version__


@pytest.mark.asyncio
async def test_health_returns_ok(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["version"] == __version__
