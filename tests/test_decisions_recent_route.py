"""Tests for GET /api/decisions/recent — grouped cascade payload (spec §3.6).

T4.4: extended to cover the new decision_kind taxonomy (delta_pushback,
daily_brief) and the new decision_kind filter query parameter.
"""

from __future__ import annotations

import json

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


@pytest.mark.asyncio
async def test_decisions_recent_unjoinable_decision_id(
    engine: None, client: AsyncClient,
) -> None:
    """AgentReportRows with a non-integer decision_id (e.g. intake session UUID)
    must appear in the response with tier/ticker/decision_kind all None and
    status defaulting to 'done' (unjoinable — no DecisionRun row exists).

    A joinable group (integer decision_id) seeded in the same request must
    still resolve tier/ticker/decision_kind from its DecisionRun row.
    """
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))

        # Joinable group: integer decision_id backed by a real DecisionRun row.
        dr = DecisionRun(
            user_id="ariel", ticker="AAPL", tier="T2",
            decision_kind="trade_proposal", status="done",
        )
        session.add(dr)
        await session.flush()

        # Rows for the joinable group.
        for role in ("news", "macro"):
            session.add(AgentReportRow(
                user_id="ariel", agent_role=role,
                decision_id=str(dr.id),
                response_text="{}", prompt_hash="h", tokens_in=5, tokens_out=5,
                cost_usd=0.01, model="claude-sonnet-4-6",
                cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
            ))

        # Unjoinable group: non-integer decision_id (intake session string).
        for role in ("news", "intake_agent"):
            session.add(AgentReportRow(
                user_id="ariel", agent_role=role,
                decision_id="intake-abc-123",
                response_text="{}", prompt_hash="h", tokens_in=3, tokens_out=3,
                cost_usd=0.005, model="claude-sonnet-4-6",
                cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
            ))

        await session.commit()

    res = await client.get("/api/decisions/recent?user_id=ariel&limit=20")
    assert res.status_code == 200, res.text
    groups = res.json()

    # Both groups must be present.
    decision_ids = {g["decision_id"] for g in groups}
    assert "intake-abc-123" in decision_ids
    assert str(dr.id) in decision_ids

    # --- Unjoinable group assertions ---
    intake_group = next(g for g in groups if g["decision_id"] == "intake-abc-123")
    assert intake_group["tier"] is None
    assert intake_group["ticker"] is None
    assert intake_group["decision_kind"] is None
    assert intake_group["status"] == "done"
    assert intake_group["agent_count"] >= 1

    # --- Joinable group assertions ---
    joinable_group = next(g for g in groups if g["decision_id"] == str(dr.id))
    assert joinable_group["tier"] == "T2"
    assert joinable_group["ticker"] == "AAPL"
    assert joinable_group["decision_kind"] == "trade_proposal"


# ---------------------------------------------------------------------------
# T4.4 — new decision kinds (delta_pushback + daily_brief) + filter param.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decisions_recent_includes_delta_pushback_kind(
    engine: None, client: AsyncClient,
) -> None:
    """T4.4: a seeded delta_pushback DecisionRun row appears in /recent
    with the correct decision_kind label and notes_json passthrough.

    Mimics the row shape T4.3 will produce when it ships (the synthesis
    pipeline doesn't generate one yet, but the endpoint must already
    handle it so T4.3 has a home immediately).
    """
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        dr = DecisionRun(
            user_id="ariel",
            ticker="(plan)",
            tier=None,
            decision_kind="delta_pushback",
            status="done",
            notes_json=json.dumps({
                "delta_item_id": "DI-2026-05-26-NVDA-trim",
                "feedback": "wait until Q1 earnings",
            }),
        )
        session.add(dr)
        await session.flush()
        # Slim agent set — bull + bear + plan_synthesizer scoped to the item.
        for role in ("bull_researcher", "bear_researcher", "plan_synthesizer"):
            session.add(AgentReportRow(
                user_id="ariel", agent_role=role,
                decision_id=str(dr.id),
                response_text="{}", prompt_hash="h",
                tokens_in=10, tokens_out=20, cost_usd=0.03,
                model="claude-opus-4-7",
                cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
            ))
        await session.commit()

    res = await client.get("/api/decisions/recent?user_id=ariel&limit=20")
    assert res.status_code == 200, res.text
    groups = res.json()
    g = next(g for g in groups if g["decision_id"] == str(dr.id))
    assert g["decision_kind"] == "delta_pushback"
    assert g["agent_count"] == 3
    # delta_pushback rows use the synthesis "(plan)" sentinel ticker (the
    # DB column is NOT NULL); the UI suppresses display of "(plan)" via
    # the !group.ticker branch when no real ticker is present.
    assert g["ticker"] == "(plan)"
    assert g["tier"] is None
    # notes_json passthrough — parsed client-side by DecisionAccordion.
    assert g["notes_json"] is not None
    parsed = json.loads(g["notes_json"])
    assert parsed["delta_item_id"] == "DI-2026-05-26-NVDA-trim"


@pytest.mark.asyncio
async def test_decisions_recent_filter_by_daily_brief_returns_only_briefs(
    engine: None, client: AsyncClient,
) -> None:
    """T4.4: ?decision_kind=daily_brief returns only daily_brief groups."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        # Mix of kinds.
        brief1 = DecisionRun(
            user_id="ariel", ticker="(plan)", tier=None,
            decision_kind="daily_brief", status="done",
            notes_json=json.dumps({"brief_date": "2026-05-25"}),
        )
        brief2 = DecisionRun(
            user_id="ariel", ticker="(plan)", tier=None,
            decision_kind="daily_brief", status="done",
            notes_json=json.dumps({"brief_date": "2026-05-26"}),
        )
        trade = DecisionRun(
            user_id="ariel", ticker="AAPL", tier="T1",
            decision_kind="trade_proposal", status="done",
        )
        pushback = DecisionRun(
            user_id="ariel", ticker="(plan)", tier=None,
            decision_kind="delta_pushback", status="done",
            notes_json=json.dumps({"delta_item_id": "DI-x"}),
        )
        session.add_all([brief1, brief2, trade, pushback])
        await session.flush()
        for dr in (brief1, brief2, trade, pushback):
            session.add(AgentReportRow(
                user_id="ariel", agent_role="news",
                decision_id=str(dr.id),
                response_text="{}", prompt_hash="h",
                tokens_in=5, tokens_out=5, cost_usd=0.01,
                model="claude-sonnet-4-6",
                cache_input_tokens=0, cache_creation_tokens=0,
                thinking_tokens=0,
            ))
        await session.commit()
        brief_ids = {str(brief1.id), str(brief2.id)}

    # Unfiltered: all four groups returned.
    res_all = await client.get("/api/decisions/recent?user_id=ariel&limit=20")
    assert res_all.status_code == 200
    assert len(res_all.json()) == 4

    # Filtered by daily_brief: only brief1 + brief2.
    res = await client.get(
        "/api/decisions/recent?user_id=ariel&limit=20&decision_kind=daily_brief"
    )
    assert res.status_code == 200, res.text
    groups = res.json()
    assert len(groups) == 2
    assert {g["decision_id"] for g in groups} == brief_ids
    for g in groups:
        assert g["decision_kind"] == "daily_brief"

    # Filtered by delta_pushback: only the pushback row.
    res2 = await client.get(
        "/api/decisions/recent?user_id=ariel&limit=20&decision_kind=delta_pushback"
    )
    assert res2.status_code == 200
    g2 = res2.json()
    assert len(g2) == 1
    assert g2[0]["decision_kind"] == "delta_pushback"

    # Filter with no matches returns empty list, not 404.
    res3 = await client.get(
        "/api/decisions/recent?user_id=ariel&limit=20&decision_kind=plan_revision"
    )
    assert res3.status_code == 200
    assert res3.json() == []


@pytest.mark.asyncio
async def test_decisions_recent_plan_synth_prefix_resolves_kind(
    engine: None, client: AsyncClient,
) -> None:
    """A plan-synthesis run whose agent_reports use the ``plan-synth-<id>``
    decision_id prefix must still join to its DecisionRun row so the row
    surfaces a non-empty decision_kind ('plan_revision') and a human
    description — instead of the all-"—" bug.
    """
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        dr = DecisionRun(
            user_id="ariel",
            ticker="(plan)",
            tier=None,
            decision_kind="plan_revision",
            status="approved",
            fund_manager_decision="APPROVE_WITH_CONDITIONS",
            proposal_id=None,
        )
        session.add(dr)
        await session.flush()
        # Synthesis agents fan out under the plan-synth-<id> decision_id.
        for role in ("plan_synthesizer", "audit", "bull_researcher"):
            session.add(AgentReportRow(
                user_id="ariel", agent_role=role,
                decision_id=f"plan-synth-{dr.id}",
                response_text="{}", prompt_hash="h",
                tokens_in=10, tokens_out=20, cost_usd=0.05,
                model="claude-opus-4-8",
                cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
            ))
        await session.commit()
        synth_did = f"plan-synth-{dr.id}"

    res = await client.get("/api/decisions/recent?user_id=ariel&limit=20")
    assert res.status_code == 200, res.text
    groups = res.json()
    g = next(g for g in groups if g["decision_id"] == synth_did)
    # The bug was decision_kind/tier/ticker all None because int("plan-synth-49")
    # failed; the prefix-aware join now resolves them.
    assert g["decision_kind"] == "plan_revision"
    assert g["ticker"] == "(plan)"
    assert g["status"] == "approved"
    assert g["agent_count"] == 3
    # Description must be non-empty and reflect real fields.
    assert g["description"]
    assert "Plan synthesis / revision" in g["description"]
    assert "FM: APPROVE_WITH_CONDITIONS" in g["description"]
    assert "3 agents" in g["description"]

    # The server-side decision_kind filter must also match prefixed runs.
    res2 = await client.get(
        "/api/decisions/recent?user_id=ariel&limit=20&decision_kind=plan_revision"
    )
    assert res2.status_code == 200, res2.text
    g2 = res2.json()
    assert len(g2) == 1
    assert g2[0]["decision_id"] == synth_did


@pytest.mark.asyncio
async def test_decisions_recent_filter_excludes_unjoinable_groups(
    engine: None, client: AsyncClient,
) -> None:
    """T4.4: groups with an unjoinable decision_id (no DecisionRun row)
    are excluded when the filter is active — they have no decision_kind
    to compare against. Without the filter they still appear.
    """
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        dr = DecisionRun(
            user_id="ariel", ticker="(plan)", tier=None,
            decision_kind="daily_brief", status="done",
            notes_json=json.dumps({"brief_date": "2026-05-26"}),
        )
        session.add(dr)
        await session.flush()
        session.add(AgentReportRow(
            user_id="ariel", agent_role="news",
            decision_id=str(dr.id),
            response_text="{}", prompt_hash="h",
            tokens_in=5, tokens_out=5, cost_usd=0.01,
            model="claude-sonnet-4-6",
            cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
        ))
        # Unjoinable group (intake-session UUID).
        session.add(AgentReportRow(
            user_id="ariel", agent_role="intake_agent",
            decision_id="intake-zzz-999",
            response_text="{}", prompt_hash="h",
            tokens_in=5, tokens_out=5, cost_usd=0.01,
            model="claude-sonnet-4-6",
            cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
        ))
        await session.commit()

    # Without filter: both groups present.
    res_all = await client.get("/api/decisions/recent?user_id=ariel&limit=20")
    decision_ids = {g["decision_id"] for g in res_all.json()}
    assert "intake-zzz-999" in decision_ids
    assert str(dr.id) in decision_ids

    # With filter: only joinable group with matching kind.
    res = await client.get(
        "/api/decisions/recent?user_id=ariel&limit=20&decision_kind=daily_brief"
    )
    assert res.status_code == 200
    groups = res.json()
    assert len(groups) == 1
    assert groups[0]["decision_id"] == str(dr.id)
