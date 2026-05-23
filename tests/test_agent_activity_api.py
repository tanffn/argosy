"""GET /api/agent-activity returns the four new Wave A fields per row.

Wave A added four columns to ``agent_reports`` via migration 0026:
``cache_input_tokens``, ``cache_creation_tokens``, ``thinking_tokens`` and
``citations_json``. The activity feed must surface them so the UI can
show cache savings, thinking cost and citation counts.

``citations_count`` is derived in the route from ``len(citations_json)``.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from argosy.state import db as db_mod
from argosy.state.models import AgentReport


@pytest.mark.asyncio
async def test_response_includes_wave_a_telemetry_fields(client: AsyncClient) -> None:
    """A seeded AgentReport row exposes all four Wave A telemetry fields."""
    async with db_mod.get_session() as session:
        session.add(
            AgentReport(
                user_id="ariel",
                agent_role="news_analyst",
                model="claude-sonnet-4-6",
                prompt_hash="0" * 64,
                response_text="hello",
                tokens_in=1000,
                tokens_out=200,
                cost_usd=0.005,
                cache_input_tokens=600,
                cache_creation_tokens=200,
                thinking_tokens=0,
                citations_json='[{"source_id":"x","cited_quote":"y"}]',
            )
        )
        await session.commit()

    resp = await client.get("/api/agent-activity?user_id=ariel&limit=10")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) >= 1
    row = rows[0]
    assert "cache_input_tokens" in row and row["cache_input_tokens"] == 600
    assert "cache_creation_tokens" in row and row["cache_creation_tokens"] == 200
    assert "thinking_tokens" in row and row["thinking_tokens"] == 0
    assert "citations_count" in row and row["citations_count"] == 1


@pytest.mark.asyncio
async def test_response_handles_null_citations_and_default_telemetry(
    client: AsyncClient,
) -> None:
    """Rows with ``citations_json=NULL`` get ``citations_count=0`` and the int
    telemetry fields fall back to their migration default of ``0``."""
    async with db_mod.get_session() as session:
        session.add(
            AgentReport(
                user_id="ariel",
                agent_role="planner",
                model="claude-sonnet-4-6",
                prompt_hash="1" * 64,
                response_text="hi",
                tokens_in=10,
                tokens_out=5,
                cost_usd=0.0,
                # cache_*/thinking_tokens omitted — should default to 0.
                citations_json=None,
            )
        )
        await session.commit()

    resp = await client.get("/api/agent-activity?user_id=ariel&limit=10")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["cache_input_tokens"] == 0
    assert row["cache_creation_tokens"] == 0
    assert row["thinking_tokens"] == 0
    assert row["citations_count"] == 0
