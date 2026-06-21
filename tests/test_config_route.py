"""GET /api/config smoke test — backend-derived home-page config counts."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from argosy.api.routes.config import derive_fleet_count


@pytest.mark.asyncio
async def test_config_returns_derived_fleet_count(client: AsyncClient) -> None:
    resp = await client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fleet_count"] == derive_fleet_count()


def test_derive_fleet_count_matches_agents_package() -> None:
    """The derived count is the live number of public agent classes.

    Mirrors the documented grep: public `class <Name>Agent` declarations under
    argosy/agents/, excluding BaseAgent and the private _ResearcherAgent helper.
    """
    count = derive_fleet_count()
    # A real fleet is a couple dozen agents — never zero, never absurd.
    assert 20 < count < 200
