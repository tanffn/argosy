"""Internal-only / admin routes (Phase 6).

  GET  /internal/health/full?user_id=  → full watchdog signals JSON.
  POST /internal/telemetry             → receiver stub for self-hosted
                                         telemetry instances.

These routes are gated by the `X-Argosy-Admin-Token` header. The token
lives in the keychain entry `argosy.admin.token` (auto-generated on
first request if absent). They are NOT linked from the dashboard nav.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from argosy.logging import get_logger
from argosy.orchestrator.watchdog import collect_signals, signals_to_dict
from argosy.secrets import get_secret, set_secret


_log = get_logger("argosy.api.internal")
router = APIRouter(prefix="/internal", tags=["internal"])


ADMIN_TOKEN_KEY = "argosy.admin.token"


def _ensure_admin_token() -> str:
    tok = get_secret(ADMIN_TOKEN_KEY)
    if tok:
        return tok
    tok = secrets.token_urlsafe(32)
    try:
        set_secret(ADMIN_TOKEN_KEY, tok)
    except Exception:  # pragma: no cover
        _log.warning("internal.admin_token.set_failed")
    return tok


def _check_admin(provided: str | None) -> None:
    expected = _ensure_admin_token()
    if not provided or provided != expected:
        raise HTTPException(status_code=401, detail="admin token required")


class TelemetryIn(BaseModel):
    bucket: str
    name: str
    fields: dict[str, Any] = {}
    timestamp: float | None = None
    user_hash: str | None = None


@router.get("/health/full")
async def full_health(
    user_id: str = Query("ariel"),
    x_argosy_admin_token: str | None = Header(default=None, alias="X-Argosy-Admin-Token"),
) -> dict[str, Any]:
    _check_admin(x_argosy_admin_token)
    sig = await collect_signals(user_id)
    return signals_to_dict(sig)


@router.post("/telemetry")
async def telemetry_receiver(
    body: TelemetryIn,
    x_argosy_admin_token: str | None = Header(default=None, alias="X-Argosy-Admin-Token"),
) -> dict[str, Any]:
    _check_admin(x_argosy_admin_token)
    _log.info(
        "telemetry.received",
        bucket=body.bucket,
        name=body.name,
        user_hash=body.user_hash,
        field_count=len(body.fields),
    )
    return {"status": "ok"}


__all__ = ["router"]
