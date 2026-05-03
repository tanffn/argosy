"""Per-tenant branding (SDD §12.4).

GET /api/branding?user_id= returns the resolved branding config. The
frontend caches it and renders the app shell (app_name, theme tokens,
logo / favicon) accordingly.

Schema (`configs/<user_id>/branding.yaml`):

    app_name: "Argosy"
    theme:
      primary: "#0ea5e9"
      accent: "#f59e0b"
    logo_url: "/logo.svg"
    favicon_url: "/favicon.ico"
    support_email: "support@argosy.app"
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from argosy.config import get_settings


router = APIRouter(tags=["branding"])


DEFAULT_APP_NAME = "Argosy"
DEFAULT_PRIMARY = "#0ea5e9"
DEFAULT_ACCENT = "#f59e0b"
DEFAULT_LOGO_URL = "/logo.svg"
DEFAULT_FAVICON_URL = "/favicon.ico"
DEFAULT_SUPPORT_EMAIL = "support@argosy.app"


class ThemeBlock(BaseModel):
    primary: str = DEFAULT_PRIMARY
    accent: str = DEFAULT_ACCENT


class BrandingDTO(BaseModel):
    app_name: str = DEFAULT_APP_NAME
    theme: ThemeBlock = Field(default_factory=ThemeBlock)
    logo_url: str = DEFAULT_LOGO_URL
    favicon_url: str = DEFAULT_FAVICON_URL
    support_email: str = DEFAULT_SUPPORT_EMAIL


def load_branding(user_id: str, configs_dir: Path | None = None) -> BrandingDTO:
    """Load `branding.yaml` for a user; fall back to Argosy defaults."""
    cfg_dir = configs_dir or get_settings().configs_dir
    path = cfg_dir / user_id / "branding.yaml"
    if not path.is_file():
        return BrandingDTO()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return BrandingDTO()
    if not isinstance(data, dict):
        return BrandingDTO()

    theme_data = data.get("theme") if isinstance(data.get("theme"), dict) else {}
    return BrandingDTO(
        app_name=str(data.get("app_name", DEFAULT_APP_NAME)),
        theme=ThemeBlock(
            primary=str(theme_data.get("primary", DEFAULT_PRIMARY)),
            accent=str(theme_data.get("accent", DEFAULT_ACCENT)),
        ),
        logo_url=str(data.get("logo_url", DEFAULT_LOGO_URL)),
        favicon_url=str(data.get("favicon_url", DEFAULT_FAVICON_URL)),
        support_email=str(data.get("support_email", DEFAULT_SUPPORT_EMAIL)),
    )


@router.get("/branding", response_model=BrandingDTO)
async def get_branding(user_id: str = Query("ariel")) -> BrandingDTO:
    return load_branding(user_id)


__all__ = ["router", "BrandingDTO", "ThemeBlock", "load_branding"]
