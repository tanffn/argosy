"""GET /health — liveness + DB ping + build info.

`git_sha` and `started_at` are captured at process start (see
`argosy/api/build_info.py`) so the UI can verify the running backend
matches the user's latest commit.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from argosy.api.build_info import GIT_SHA, STARTED_AT, VERSION
from argosy.state.db import get_engine

router = APIRouter()


class HealthResponse(BaseModel):
    status: Literal["ok", "error"]
    db: Literal["ok", "error"]
    version: str
    git_sha: str
    started_at: str  # ISO 8601 UTC


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
