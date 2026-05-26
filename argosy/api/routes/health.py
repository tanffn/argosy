"""GET /health — liveness + DB ping + build info.

`git_sha` and `started_at` are captured at process start (see
`argosy/api/build_info.py`) so the UI can verify the running backend
matches the user's latest commit.

Also hosts GET /system/db-size — small home-page tile data source.
Mounted under the health router because the router is the only one
exposed at both root and /api/* (so a future watchdog or one-shot
curl call can hit it without the Next.js proxy).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from argosy.api.build_info import GIT_SHA, STARTED_AT, VERSION
from argosy.config import get_settings
from argosy.state.db import get_engine

router = APIRouter()


class HealthResponse(BaseModel):
    status: Literal["ok", "error"]
    db: Literal["ok", "error"]
    version: str
    git_sha: str
    started_at: str  # ISO 8601 UTC


class DbSizeResponse(BaseModel):
    # Bytes is the authoritative value; `human` is a convenience for tiles
    # so the UI doesn't have to recompute its own SI/binary formatter.
    size_bytes: int
    size_human: str


def _format_bytes(n: int) -> str:
    if n <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(n)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    # Single decimal for KB+, integer for B / large round numbers.
    if idx == 0 or value >= 10:
        return f"{value:.0f} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    db_status: Literal["ok", "error"] = "ok"
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_status = "error"

    overall: Literal["ok", "error"] = "ok" if db_status == "ok" else "error"
    return HealthResponse(
        status=overall,
        db=db_status,
        version=VERSION,
        git_sha=GIT_SHA,
        started_at=STARTED_AT.isoformat(),
    )


@router.get("/system/db-size", response_model=DbSizeResponse)
def db_size() -> DbSizeResponse:
    """Return the SQLite state DB file size for the SYSTEM tile on the home page.

    Returns 0 bytes when the DB file does not yet exist (e.g. fresh
    install, ephemeral test fixture using an in-memory engine).
    """
    settings = get_settings()
    db_file = settings.db_file
    if not db_file.is_file():
        return DbSizeResponse(size_bytes=0, size_human="0 B")
    size = db_file.stat().st_size
    return DbSizeResponse(size_bytes=size, size_human=_format_bytes(size))
