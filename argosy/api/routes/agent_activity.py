"""GET /api/agent-activity — paginated feed of recent agent runs."""

from __future__ import annotations

import json
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
    # Wave A — Anthropic Messages API telemetry (migration 0026).
    # ``citations_count`` is derived from ``len(citations_json)`` in the
    # route handler; defaults match the migration's server_default="0".
    cache_input_tokens: int = 0
    cache_creation_tokens: int = 0
    thinking_tokens: int = 0
    citations_count: int = 0
    # Wave B-UI — drawer fields exposed so the detail drawer can render
    # full response text, citation list and prompt identity.
    response_text: str = ""
    citations_json: str | None = None
    prompt_hash: str = ""
    # Wave B-UI Task 5 — grouping key for intake-session agents (mutually
    # exclusive with decision_id in practice; both may be null for
    # standalone / cadence agents).
    intake_session_id: str | None = None
    # Wave B-UI Task 9 — lightweight source previews so the Sources tab can
    # render real data without fetching the full content blobs.
    # Each entry: {source_id, body_chars (full length), body_head (≤150 chars)}.
    sources_preview: list[dict[str, Any]] = []


class AgentActivityResponse(BaseModel):
    rows: list[AgentActivityRow]
    next_since: str | None = None


@router.get("", response_model=AgentActivityResponse)
async def get_agent_activity(
    user_id: str = Query("ariel"),
    since: str | None = Query(None, description="ISO 8601 datetime; rows newer than this."),
    limit: int = Query(10, ge=1, le=500),
    detail: bool = Query(True, description="Include heavy fields (response_text, citations_json, prompt_hash, sources_preview). Pass detail=false for lightweight cost-only fetches."),
) -> AgentActivityResponse:
    """Return up to `limit` most-recent agent reports for `user_id`.

    `since` filters strictly newer than the given timestamp (good for
    incremental polling). `next_since` returns the latest row's
    `created_at` so the caller can keep pulling forward.

    `detail=false` omits heavy per-row fields (response_text, citations_json,
    prompt_hash, sources_preview) for lightweight callers that only need cost
    or timing data (e.g. the home-page monthly-cost summation).
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
        # citations_json is a stored JSON array (or NULL). Length of the
        # decoded list is more useful to the UI than the raw blob.
        citations_count = (
            len(json.loads(r.citations_json)) if r.citations_json else 0
        )
        # Heavy fields: only populated when detail=True.
        if detail:
            # sources_json is a stored JSON array of {source_id, content} (or NULL).
            # Build lightweight previews: truncate content to 150 chars for body_head,
            # record full length as body_chars.  Defensive: on any parse error return [].
            sources_preview: list[dict[str, Any]] = []
            if r.sources_json:
                try:
                    raw_sources = json.loads(r.sources_json)
                    if isinstance(raw_sources, list):
                        for entry in raw_sources:
                            sid = entry.get("source_id", "")
                            content = entry.get("content", "")
                            sources_preview.append({
                                "source_id": sid,
                                "body_chars": len(content),
                                "body_head": content[:150],
                            })
                except Exception:  # noqa: BLE001 — malformed JSON or unexpected shape
                    sources_preview = []
            row_response_text = r.response_text or ""
            row_citations_json = r.citations_json
            row_prompt_hash = r.prompt_hash or ""
        else:
            sources_preview = []
            row_response_text = ""
            row_citations_json = None
            row_prompt_hash = ""
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
                cache_input_tokens=r.cache_input_tokens or 0,
                cache_creation_tokens=r.cache_creation_tokens or 0,
                thinking_tokens=r.thinking_tokens or 0,
                citations_count=citations_count,
                response_text=row_response_text,
                citations_json=row_citations_json,
                prompt_hash=row_prompt_hash,
                intake_session_id=r.intake_session_id,
                sources_preview=sources_preview,
            )
        )
    if out:
        next_since = out[0].created_at
    return AgentActivityResponse(rows=out, next_since=next_since)


__all__ = ["router"]
