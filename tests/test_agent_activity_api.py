"""GET /api/agent-activity returns the four new Wave A fields per row.

Wave A added four columns to ``agent_reports`` via migration 0026:
``cache_input_tokens``, ``cache_creation_tokens``, ``thinking_tokens`` and
``citations_json``. The activity feed must surface them so the UI can
show cache savings, thinking cost and citation counts.

``citations_count`` is derived in the route from ``len(citations_json)``.

Wave B-UI Task 4 adds ``response_text``, ``citations_json``, and
``prompt_hash`` to the payload so the AgentDetailDrawer can render them.
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


@pytest.mark.asyncio
async def test_response_includes_wave_b_drawer_fields(client: AsyncClient) -> None:
    """Wave B-UI Task 4: response_text, citations_json, and prompt_hash round-trip."""
    citations = '[{"source_id":"s1","claim_text":"NVDA is bullish","cited_quote":"price up"}]'
    hash_val = "a" * 64
    async with db_mod.get_session() as session:
        session.add(
            AgentReport(
                user_id="ariel",
                agent_role="trader",
                model="claude-sonnet-4-6",
                prompt_hash=hash_val,
                response_text="NVDA looks strong; buy 10 shares.",
                tokens_in=500,
                tokens_out=80,
                cost_usd=0.003,
                citations_json=citations,
            )
        )
        await session.commit()

    resp = await client.get("/api/agent-activity?user_id=ariel&limit=10")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) >= 1
    row = rows[0]
    assert row["response_text"] == "NVDA looks strong; buy 10 shares."
    assert row["citations_json"] == citations
    assert row["prompt_hash"] == hash_val


# ---------------------------------------------------------------------------
# Wave B-UI Task 9 — sources_preview round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sources_preview_round_trips_with_truncation(
    client: AsyncClient,
) -> None:
    """sources_json stored on AgentReport is exposed as sources_preview in the API.

    Long content is truncated to 150 chars for body_head; body_chars reflects
    the full original length.
    """
    import json

    long_content = "x" * 500
    short_content = "short content"
    sources_json = json.dumps([
        {"source_id": "domain/tax/il.md", "content": long_content},
        {"source_id": "news/AAPL", "content": short_content},
    ])
    async with db_mod.get_session() as session:
        session.add(
            AgentReport(
                user_id="ariel",
                agent_role="synthesizer",
                model="claude-opus-4-5",
                prompt_hash="c" * 64,
                response_text="Tax report done.",
                tokens_in=300,
                tokens_out=60,
                cost_usd=0.002,
                sources_json=sources_json,
            )
        )
        await session.commit()

    resp = await client.get("/api/agent-activity?user_id=ariel&limit=10")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) >= 1
    row = rows[0]

    assert "sources_preview" in row
    previews = row["sources_preview"]
    assert len(previews) == 2

    # First entry: long content — body_chars=500, body_head truncated to 150.
    long_preview = next(p for p in previews if p["source_id"] == "domain/tax/il.md")
    assert long_preview["body_chars"] == 500
    assert len(long_preview["body_head"]) <= 150
    assert long_preview["body_head"] == "x" * 150

    # Second entry: short content — body_chars=13, body_head == full content.
    short_preview = next(p for p in previews if p["source_id"] == "news/AAPL")
    assert short_preview["body_chars"] == len(short_content)
    assert short_preview["body_head"] == short_content


@pytest.mark.asyncio
async def test_detail_false_omits_heavy_fields(client: AsyncClient) -> None:
    """detail=false returns empty/null heavy fields regardless of stored data.

    The home page fetches limit=500 rows for monthly-cost summation and only
    needs cost_usd + created_at.  Sending full response_text / citations_json
    on every row would be multi-MB per refresh; detail=false drops them.
    """
    import json as _json

    citations = _json.dumps([{"source_id": "s1", "cited_quote": "quote"}])
    async with db_mod.get_session() as session:
        session.add(
            AgentReport(
                user_id="ariel",
                agent_role="fund_manager",
                model="claude-opus-4-5",
                prompt_hash="e" * 64,
                response_text="Heavy response text that should be omitted.",
                tokens_in=400,
                tokens_out=70,
                cost_usd=0.004,
                citations_json=citations,
            )
        )
        await session.commit()

    resp = await client.get("/api/agent-activity?user_id=ariel&limit=10&detail=false")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) >= 1
    row = rows[0]
    assert row["response_text"] == ""
    assert row["citations_json"] is None
    assert row["sources_preview"] == []
    assert row["prompt_hash"] == ""
    # cost_usd and created_at must still be present.
    assert row["cost_usd"] == pytest.approx(0.004, rel=1e-3)
    assert "created_at" in row


@pytest.mark.asyncio
async def test_sources_preview_null_sources_returns_empty_list(
    client: AsyncClient,
) -> None:
    """Rows with sources_json=NULL get sources_preview=[] in the response."""
    async with db_mod.get_session() as session:
        session.add(
            AgentReport(
                user_id="ariel",
                agent_role="bull",
                model="claude-opus-4-5",
                prompt_hash="d" * 64,
                response_text="Bull thesis.",
                tokens_in=200,
                tokens_out=50,
                cost_usd=0.001,
                sources_json=None,
            )
        )
        await session.commit()

    resp = await client.get("/api/agent-activity?user_id=ariel&limit=10")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) >= 1
    row = rows[0]
    assert row["sources_preview"] == []
