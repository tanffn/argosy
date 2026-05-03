"""GET /api/agent-activity — paginated feed of recent agent runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import desc, select

from argosy.state import db as db_mod
from argosy.state.models import AgentReport

router = APIRouter(prefix="/agent-activity", tags=["agent-activity"])


class AgentActivityRow(BaseModel):
    id: int
    user_id: str
    agent_role: str
    decision_id: str | None
    model: str
    confidence: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    created_at: str


class AgentActivityResponse(BaseModel):
    rows: list[AgentActivityRow]
    next_since: str | None = None


@router.get("", response_model=AgentActivityResponse)
async def get_agent_activity(
    user_id: str = Query("ariel"),
    since: str | None = Query(None, description="ISO 8601 datetime; rows newer than this."),
    limit: int = Query(10, ge=1, le=200),
) -> AgentActivityResponse:
    """Return up to `limit` most-recent agent reports for `user_id`.

    `since` filters strictly newer than the given timestamp (good for
    incremental polling). `next_since` returns the latest row's
    `created_at` so the caller can keep pulling forward.
    """
    async with db_mod.get_session() as session:
        stmt = (
            select(AgentReport)
            .where(AgentReport.user_id == user_id)
            .order_by(desc(AgentReport.created_at))
            .limit(limit)
        )
        if since:
            try:
                cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
                # SQLite stores tz-naive datetimes; compare on a naive
                # cutoff so the query plan works the same regardless of
                # DB timezone storage.
                if cutoff.tzinfo is not None:
                    cutoff = cutoff.astimezone(cutoff.tzinfo).replace(tzinfo=None)
                stmt = stmt.where(AgentReport.created_at > cutoff)
            except ValueError:  # pragma: no cover - defensive
                pass
        rows: list[Any] = (await session.execute(stmt)).scalars().all()

    out: list[AgentActivityRow] = []
    next_since: str | None = None
    for r in rows:
        out.append(
            AgentActivityRow(
                id=r.id,
                user_id=r.user_id,
                agent_role=r.agent_role,
                decision_id=r.decision_id,
                model=r.model,
                confidence=r.confidence,
                tokens_in=r.tokens_in,
                tokens_out=r.tokens_out,
                cost_usd=float(r.cost_usd or 0),
                created_at=r.created_at.isoformat(),
            )
        )
    if out:
        next_since = out[0].created_at
    return AgentActivityResponse(rows=out, next_since=next_since)


__all__ = ["router"]
