"""Tests for GET /api/decisions/recent — grouped cascade payload (spec §3.6)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport as AgentReportRow,
    DecisionRun,
    User,
)


@pytest.mark.asyncio
async def test_decisions_recent_groups_two_decisions(
    engine: None, client: AsyncClient,
) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        # Decision 1: trade_proposal for AAPL, T2, with 4 agents
        dr1 = DecisionRun(
            user_id="ariel", ticker="AAPL", tier="T2",
            decision_kind="trade_proposal", status="done",
        )
        # Decision 2: trade_proposal for MSFT, T1, with 4 agents
        dr2 = DecisionRun(
            user_id="ariel", ticker="MSFT", tier="T1",
            decision_kind="trade_proposal", status="done",
        )
        session.add_all([dr1, dr2])
        await session.flush()

        for role in ("news", "macro", "fundamentals", "trader"):
            session.add(AgentReportRow(
                user_id="ariel", agent_role=role,
                decision_id=str(dr1.id),
                response_text="{}", prompt_hash="h", tokens_in=10, tokens_out=20,
                cost_usd=0.01, model="claude-sonnet-4-6",
                cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
            ))
            session.add(AgentReportRow(
                user_id="ariel", agent_role=role,
                decision_id=str(dr2.id),
                response_text="{}", prompt_hash="h", tokens_in=10, tokens_out=20,
                cost_usd=0.02, model="claude-sonnet-4-6",
                cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
            ))
        await session.commit()

    res = await client.get("/api/decisions/recent?user_id=ariel&limit=20")
    assert res.status_code == 200, res.text
    groups = res.json()
    assert len(groups) == 2

    # find dr1 group
    g1 = next(g for g in groups if g["decision_id"] == str(dr1.id))
    assert g1["ticker"] == "AAPL"
    assert g1["tier"] == "T2"
    assert g1["agent_count"] == 4
    assert g1["total_cost_usd"] == pytest.approx(0.04, rel=1e-3)
    assert len(g1["agent_runs"]) == 4
    assert {r["agent_role"] for r in g1["agent_runs"]} == {"news", "macro", "fundamentals", "trader"}

    g2 = next(g for g in groups if g["decision_id"] == str(dr2.id))
    assert g2["ticker"] == "MSFT"
    assert g2["total_cost_usd"] == pytest.approx(0.08, rel=1e-3)


@pytest.mark.asyncio
async def test_decisions_recent_omits_null_decision_id(
    engine: None, client: AsyncClient,
) -> None:
    """Rows with NULL decision_id must not appear in the response (standalone omit policy)."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        dr = DecisionRun(
            user_id="ariel", ticker="TSLA", tier="T1",
            decision_kind="trade_proposal", status="done",
        )
        session.add(dr)
        await session.flush()

        # One row with a real decision_id, one with NULL
        session.add(AgentReportRow(
            user_id="ariel", agent_role="news",
            decision_id=str(dr.id),
            response_text="{}", prompt_hash="h", tokens_in=5, tokens_out=5,
            cost_usd=0.01, model="claude-sonnet-4-6",
            cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
        ))
        session.add(AgentReportRow(
            user_id="ariel", agent_role="macro",
            decision_id=None,
            response_text="{}", prompt_hash="h", tokens_in=5, tokens_out=5,
            cost_usd=0.01, model="claude-sonnet-4-6",
            cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
        ))
        await session.commit()

    res = await client.get("/api/decisions/recent?user_id=ariel&limit=20")
    assert res.status_code == 200, res.text
    groups = res.json()
    # Only the group with a real decision_id should appear
    assert len(groups) == 1
    assert groups[0]["decision_id"] == str(dr.id)


@pytest.mark.asyncio
async def test_decisions_recent_limit_respected(
    engine: None, client: AsyncClient,
) -> None:
    """limit=1 must return only the most recent decision group."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        dr1 = DecisionRun(
            user_id="ariel", ticker="AAPL", tier="T2",
            decision_kind="trade_proposal", status="done",
        )
        dr2 = DecisionRun(
            user_id="ariel", ticker="MSFT", tier="T1",
            decision_kind="trade_proposal", status="done",
        )
        session.add_all([dr1, dr2])
        await session.flush()

        session.add(AgentReportRow(
            user_id="ariel", agent_role="news",
            decision_id=str(dr1.id),
            response_text="{}", prompt_hash="h", tokens_in=5, tokens_out=5,
            cost_usd=0.01, model="claude-sonnet-4-6",
            cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
        ))
        session.add(AgentReportRow(
            user_id="ariel", agent_role="news",
            decision_id=str(dr2.id),
            response_text="{}", prompt_hash="h", tokens_in=5, tokens_out=5,
            cost_usd=0.01, model="claude-sonnet-4-6",
            cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
        ))
        await session.commit()

    res = await client.get("/api/decisions/recent?user_id=ariel&limit=1")
    assert res.status_code == 200, res.text
    groups = res.json()
    assert len(groups) == 1
