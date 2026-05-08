"""GET /health smoke test."""

from __future__ import annotations

import re

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


@pytest.mark.asyncio
async def test_health_includes_git_sha_and_started_at(client: AsyncClient) -> None:
    """Build info: git_sha + started_at populated by argosy.api.build_info."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()

    assert "git_sha" in body
    # Either a real short SHA (hex chars) or the documented "unknown" fallback.
    assert isinstance(body["git_sha"], str)
    assert body["git_sha"] == "unknown" or re.match(r"^[0-9a-f]{4,40}$", body["git_sha"])

    assert "started_at" in body
    # ISO 8601 with timezone — sanity check the prefix and presence of T.
    assert isinstance(body["started_at"], str)
    assert "T" in body["started_at"]
    assert body["started_at"].endswith("+00:00") or body["started_at"].endswith("Z")
