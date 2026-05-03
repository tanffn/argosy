"""Phase 6: branding endpoint + loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from argosy.api.routes.branding import (
    DEFAULT_APP_NAME,
    DEFAULT_PRIMARY,
    load_branding,
)


def test_default_branding_when_no_yaml(tmp_path: Path) -> None:
    out = load_branding("nobody", configs_dir=tmp_path)
    assert out.app_name == DEFAULT_APP_NAME
    assert out.theme.primary == DEFAULT_PRIMARY


def test_branding_yaml_overrides(tmp_path: Path) -> None:
    cfg = tmp_path / "alice"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "branding.yaml").write_text(
        """
app_name: Pilot Capital
theme:
  primary: "#112233"
  accent: "#aabbcc"
logo_url: /alice/logo.svg
favicon_url: /alice/favicon.ico
support_email: hello@pilot.example
""",
        encoding="utf-8",
    )
    out = load_branding("alice", configs_dir=tmp_path)
    assert out.app_name == "Pilot Capital"
    assert out.theme.primary == "#112233"
    assert out.theme.accent == "#aabbcc"
    assert out.logo_url == "/alice/logo.svg"
    assert out.support_email == "hello@pilot.example"


@pytest.mark.asyncio
async def test_branding_route_returns_defaults(client: AsyncClient) -> None:
    r = await client.get("/api/branding?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["app_name"] == DEFAULT_APP_NAME
    assert body["theme"]["primary"] == DEFAULT_PRIMARY
