"""TestClient-driven API route checks (Phase 2).

Seeds a DB with users / plan_versions / agent_reports / daily_briefs and
verifies each new route returns the expected shape.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport,
    DailyBrief,
    PlanCritique,
    PlanVersion,
    User,
)


async def _seed_full_user() -> int:
    """Insert one user with one plan, one critique, one brief, one agent report."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.flush()

        plan = PlanVersion(
            user_id="ariel",
            version_label="v2.0",
            source_path="/tmp/p.md",
            raw_markdown="# Plan\n\nNVDA target 15%.\n",
        )
        session.add(plan)
        await session.flush()

        critique_payload = {
            "plan_label": "v2.0",
            "snapshot_label": "test",
            "overall_summary": "NVDA over cap.",
            "confidence": "MEDIUM",
            "cited_sources": ["domain_knowledge/x.md"],
            "findings": [],
        }
        session.add(
            PlanCritique(
                user_id="ariel",
                plan_version_id=plan.id,
                critique_json=json.dumps(critique_payload),
                model="claude-sonnet-4-6",
            )
        )

        session.add(
            DailyBrief(
                user_id="ariel",
                run_at=datetime(2026, 5, 2, 9, 0, tzinfo=timezone.utc),
                summary_text="=== DAILY BRIEF ===\nMacro regime: neutral",
                news_report_json='{"top_line":"NVDA news","per_ticker":{},'
                                 '"materiality_scores":{},"confidence":"MEDIUM",'
                                 '"cited_sources":["x"]}',
                macro_report_json='{"regime":"neutral","drivers":[],'
                                   '"key_metrics":{},"summary":"calm",'
                                   '"confidence":"MEDIUM","cited_sources":["x"]}',
                concentration_report_json='{"breaches":[],"deltas_vs_target":{},'
                                          '"nvda_pace":{"shares_sold_ytd":0,'
                                          '"target_shares_ytd":0,'
                                          '"delta_shares":0,"on_track":true},'
                                          '"summary":"ok","confidence":"HIGH",'
                                          '"cited_sources":["x"]}',
                plan_delta_json='{"findings":[],"plan_label":"v2.0",'
                                '"snapshot_label":"d","overall_summary":"ok",'
                                '"confidence":"MEDIUM","cited_sources":["x"]}',
            )
        )

        session.add(
            AgentReport(
                user_id="ariel",
                agent_role="news",
                decision_id=None,
                prompt_hash="0" * 64,
                response_text="hello",
                tokens_in=100,
                tokens_out=200,
                cost_usd=0.001,
                model="claude-sonnet-4-6",
                confidence="MEDIUM",
            )
        )

        await session.commit()
        return plan.id


@pytest.mark.asyncio
async def test_health_still_works(client: AsyncClient) -> None:
    """Phase 0 route still works after Phase 2 changes."""
    res = await client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in ("ok", "error")
    assert body["db"] in ("ok", "error")


@pytest.mark.asyncio
async def test_plan_current_returns_latest(client: AsyncClient) -> None:
    await _seed_full_user()
    res = await client.get("/api/plan/current?user_id=ariel")
    assert res.status_code == 200
    body = res.json()
    assert body["version_label"] == "v2.0"
    assert "NVDA target" in body["raw_markdown"]
    assert body["latest_critique_json"]["overall_summary"] == "NVDA over cap."


@pytest.mark.asyncio
async def test_plan_current_empty_when_no_plan(client: AsyncClient) -> None:
    res = await client.get("/api/plan/current?user_id=nobody")
    assert res.status_code == 200
    body = res.json()
    assert body["plan_version_id"] is None
    assert body["raw_markdown"] == ""


@pytest.mark.asyncio
async def test_daily_brief_latest(client: AsyncClient) -> None:
    await _seed_full_user()
    res = await client.get("/api/daily-brief/latest?user_id=ariel")
    assert res.status_code == 200
    body = res.json()
    assert body is not None
    assert body["user_id"] == "ariel"
    assert body["macro_report"]["regime"] == "neutral"
    assert "DAILY BRIEF" in body["summary_text"]


@pytest.mark.asyncio
async def test_daily_brief_latest_none(client: AsyncClient) -> None:
    res = await client.get("/api/daily-brief/latest?user_id=nobody")
    assert res.status_code == 200
    assert res.json() is None


@pytest.mark.asyncio
async def test_agent_activity(client: AsyncClient) -> None:
    await _seed_full_user()
    res = await client.get("/api/agent-activity?user_id=ariel&limit=5")
    assert res.status_code == 200
    body = res.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["agent_role"] == "news"
    assert body["next_since"] is not None


@pytest.mark.asyncio
async def test_agent_activity_pagination_with_since(client: AsyncClient) -> None:
    await _seed_full_user()
    # Use a future cutoff: should yield nothing. Pass via `params=` so
    # the `+` in the timezone offset is properly URL-encoded.
    res = await client.get(
        "/api/agent-activity",
        params={"user_id": "ariel", "since": "2099-01-01T00:00:00+00:00"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["rows"] == []


@pytest.mark.asyncio
async def test_portfolio_snapshot_endpoint_returns_shape(client: AsyncClient) -> None:
    """Endpoint returns valid shape even when no TSV is present."""
    res = await client.get("/api/portfolio/snapshot?user_id=ariel")
    assert res.status_code == 200
    body = res.json()
    # Must contain the structural keys regardless of data presence.
    for key in ("snapshot_date", "fx_usd_nis", "total_usd_value_k", "positions", "allocations"):
        assert key in body
