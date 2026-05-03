"""GET /api/daily-brief/latest — most recent daily brief for a user."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import desc, select

from argosy.state import db as db_mod
from argosy.state.models import DailyBrief

router = APIRouter(prefix="/daily-brief", tags=["daily-brief"])


class DailyBriefDTO(BaseModel):
    id: int
    user_id: str
    run_at: str
    summary_text: str
    news_report: dict[str, Any] | None
    macro_report: dict[str, Any] | None
    concentration_report: dict[str, Any] | None
    plan_delta: dict[str, Any] | None


def _parse(blob: str) -> dict[str, Any] | None:
    if not blob:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:  # pragma: no cover - defensive
        return None


@router.get("/latest", response_model=DailyBriefDTO | None)
async def get_latest_brief(
    user_id: str = Query("ariel"),
) -> DailyBriefDTO | None:
    async with db_mod.get_session() as session:
        row = (
            await session.execute(
                select(DailyBrief)
                .where(DailyBrief.user_id == user_id)
                .order_by(desc(DailyBrief.run_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return DailyBriefDTO(
            id=row.id,
            user_id=row.user_id,
            run_at=row.run_at.isoformat(),
            summary_text=row.summary_text,
            news_report=_parse(row.news_report_json),
            macro_report=_parse(row.macro_report_json),
            concentration_report=_parse(row.concentration_report_json),
            plan_delta=_parse(row.plan_delta_json),
        )


__all__ = ["router"]
