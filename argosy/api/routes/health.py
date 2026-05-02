"""GET /health — liveness + DB ping."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from argosy import __version__
from argosy.state.db import get_engine

router = APIRouter()


class HealthResponse(BaseModel):
    status: Literal["ok", "error"]
    db: Literal["ok", "error"]
    version: str


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
    return HealthResponse(status=overall, db=db_status, version=__version__)
