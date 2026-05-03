"""Settings screen API (SDD §11.1 #10, Phase 7).

Endpoints:
  - GET   /api/settings?user_id=...     — read agent_settings.yaml
  - PATCH /api/settings                 — patch & save agent_settings.yaml
  - POST  /internal/cost-guard/override — lift the cost-guard pause

Sections covered:
  - cadence scheduling
  - tier thresholds
  - execution mode per account (global default + limited account)
  - model overrides per agent role
  - alert channels (email + telegram placeholder)
  - backup destination
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query
from pydantic import BaseModel

from argosy.agent_settings import AgentSettings, load_agent_settings, save_agent_settings
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.orchestrator.cost_guard import get_cost_guard

_log = get_logger("argosy.api.settings")
router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
async def get_settings_endpoint(user_id: str = Query("ariel")) -> dict[str, Any]:
    """Return the AgentSettings for `user_id` as JSON."""
    settings = load_agent_settings(user_id)
    return settings.model_dump(mode="json")


class PatchRequest(BaseModel):
    user_id: str = "ariel"
    patch: dict[str, Any]


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `src` into `dst`. Returns `dst` in place for convenience."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


@router.patch("")
async def patch_settings(req: PatchRequest = Body(...)) -> dict[str, Any]:
    """Patch the user's AgentSettings.yaml. Returns the new full state."""
    current = load_agent_settings(req.user_id)
    payload = current.model_dump(mode="json")
    _deep_merge(payload, req.patch)
    try:
        new_settings = AgentSettings.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"validation error: {exc}") from exc
    save_agent_settings(req.user_id, new_settings)
    return new_settings.model_dump(mode="json")


# ---- Cost guard override (referenced by SDD §14.7) -------------------

# Mounted on the `internal` router at `/internal/cost-guard/override` —
# we expose a constructor so the main app can include it without
# duplicating admin-token boilerplate.

cost_guard_router = APIRouter(prefix="/internal/cost-guard", tags=["internal"])


class CostGuardOverrideRequest(BaseModel):
    user_id: str = "ariel"
    minutes: int = 60


@cost_guard_router.post("/override")
async def cost_guard_override(
    req: CostGuardOverrideRequest = Body(...),
    x_argosy_admin_token: str | None = Header(default=None, alias="X-Argosy-Admin-Token"),
) -> dict[str, Any]:
    # Reuse the admin-token guard from the internal router.
    from argosy.api.routes.internal import _check_admin

    _check_admin(x_argosy_admin_token)

    guard = get_cost_guard(user_id=req.user_id)
    expiry = guard.set_override(minutes=req.minutes)
    await record_audit_event(
        user_id=req.user_id,
        event_type="cost_guard.override",
        entity_type="cost_guard",
        entity_id=req.user_id,
        payload={"minutes": req.minutes, "expires_at": expiry.isoformat()},
    )
    _log.info(
        "cost_guard.override", user_id=req.user_id, minutes=req.minutes, expires_at=expiry.isoformat()
    )
    return {"status": "ok", "expires_at": expiry.isoformat()}


__all__ = ["router", "cost_guard_router"]
