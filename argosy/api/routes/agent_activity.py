"""GET /api/agent-activity — paginated feed of recent agent runs."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select

from argosy.state import db as db_mod
from argosy.state.models import AgentReport

router = APIRouter(prefix="/agent-activity", tags=["agent-activity"])


def build_sources_preview(sources_json: str | None) -> list[dict[str, Any]]:
    """Turn a stored ``sources_json`` blob into the wire ``sources_preview`` list.

    Each entry: ``{source_id, body_chars (full length), body_head (≤150 chars)}``.
    Defensive — malformed JSON or unexpected shape returns ``[]`` rather than raising.
    Shared by ``/api/agent-activity`` and ``/api/decisions/recent``.
    """
    if not sources_json:
        return []
    try:
        raw_sources = json.loads(sources_json)
    except Exception:  # noqa: BLE001 — malformed JSON
        return []
    if not isinstance(raw_sources, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw_sources:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content", "")
        out.append({
            "source_id": entry.get("source_id", ""),
            "body_chars": len(content),
            "body_head": content[:150],
        })
    return out


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
    # Wave B-UI follow-up Item 2 — uuid4 correlation id from BaseAgent.run()
    # (migration 0028). NULL for rows persisted before this migration.
    # Always included (not a heavy field) so the hook can do O(1) WS↔DB lookup
    # regardless of the detail= flag.
    run_correlation_id: str | None = None


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
            sources_preview = build_sources_preview(r.sources_json)
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
                # Wave B-UI follow-up Item 2 — always include regardless of
                # detail flag (tiny string; needed for O(1) WS↔DB lookup).
                run_correlation_id=r.run_correlation_id,
            )
        )
    if out:
        next_since = out[0].created_at
    return AgentActivityResponse(rows=out, next_since=next_since)


class AgentPromptResponse(BaseModel):
    id: int
    system_prompt: str
    user_prompt: str


@router.get("/{report_id}/prompt", response_model=AgentPromptResponse)
async def get_agent_prompt(
    report_id: int,
    user_id: str = Query("ariel"),
) -> AgentPromptResponse:
    """Return the full system + user prompt for one agent run.

    Separate from the list endpoint because prompts are large (10-100KB each)
    and would bloat the cascade / accordion fetches. Drawer fetches on-demand
    when the Prompt tab opens.

    404 when not found or owned by a different user. Returns empty strings
    (NOT 404) for rows persisted before migration 0029 — UI shows a
    "no prompt captured" empty state.
    """
    async with db_mod.get_session() as session:
        row = (await session.execute(
            select(AgentReport).where(AgentReport.id == report_id)
        )).scalar_one_or_none()
    if row is None or row.user_id != user_id:
        raise HTTPException(404, detail="agent report not found")
    return AgentPromptResponse(
        id=row.id,
        system_prompt=row.system_prompt or "",
        user_prompt=row.user_prompt or "",
    )


__all__ = ["router", "build_sources_preview", "AgentPromptResponse"]
