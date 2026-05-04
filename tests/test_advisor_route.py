"""Advisor API route tests — /turn, /gaps, and intake-alias backwards-compat."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from argosy.agents.advisor import AdvisorAgent
from argosy.agents.base import ModelCall
from argosy.api.routes.advisor import (
    classify_mode,
    reset_advisor_agent_factory,
    set_advisor_agent_factory,
)
from argosy.state import db as db_mod
from argosy.state.models import AgentReport as AgentReportRow
from argosy.state.models import User, UserContext


def _canned(question: str, **overrides) -> dict:
    base = {
        "stage": "stage_1",
        "question_for_user": question,
        "context_updates": [],
        "stage_complete": False,
        "next_stage": None,
        "confidence": "MEDIUM",
        "cited_sources": [],
        "notes_for_orchestrator": "",
        "mode": "gap_driven",
    }
    base.update(overrides)
    return base


def _factory(canned: dict):
    def _make(user_id: str):
        class _M(AdvisorAgent):
            async def _call_model(self, *, system: str, user: str) -> ModelCall:
                return ModelCall(
                    text=json.dumps(canned),
                    tokens_in=80,
                    tokens_out=120,
                    model=self.model,
                )

        return _M(user_id=user_id)

    return _make


# ----------------------------------------------------------------------
# classify_mode unit
# ----------------------------------------------------------------------


def test_classify_mode_empty_is_gap_driven() -> None:
    assert classify_mode("") == "gap_driven"
    assert classify_mode("   ") == "gap_driven"


def test_classify_mode_question_is_user_driven() -> None:
    assert classify_mode("What's my tax bracket?") == "user_driven"


def test_classify_mode_statement_is_user_driven() -> None:
    """Even a non-question statement counts as a user-driven turn — the
    advisor should acknowledge + log + ask a related follow-up."""
    assert classify_mode("My salary just went up to 750k NIS.") == "user_driven"


# ----------------------------------------------------------------------
# /api/advisor/turn
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advisor_turn_gap_driven_default(
    engine: None, client: AsyncClient
) -> None:
    set_advisor_agent_factory(_factory(_canned("What is your tax residency?")))
    try:
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(UserContext(user_id="ariel", current_stage="stage_1"))
            await session.commit()

        res = await client.post(
            "/api/advisor/turn",
            json={"user_id": "ariel", "last_user_message": ""},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["mode"] == "gap_driven"
        assert "tax residency" in body["question_for_user"].lower()
        assert body["intake_session_id"]
    finally:
        reset_advisor_agent_factory()


@pytest.mark.asyncio
async def test_advisor_turn_user_driven_when_question(
    engine: None, client: AsyncClient
) -> None:
    set_advisor_agent_factory(
        _factory(_canned("Israeli tax brackets are…", mode="user_driven"))
    )
    try:
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(
                UserContext(
                    user_id="ariel",
                    current_stage="stage_1",
                    identity_yaml="tax_residency: israel\n",
                )
            )
            await session.commit()

        res = await client.post(
            "/api/advisor/turn",
            json={
                "user_id": "ariel",
                "last_user_message": "What's my surtax threshold?",
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["mode"] == "user_driven"
    finally:
        reset_advisor_agent_factory()


@pytest.mark.asyncio
async def test_advisor_turn_persists_agent_report_with_advisor_role(
    engine: None, client: AsyncClient
) -> None:
    set_advisor_agent_factory(_factory(_canned("Q?")))
    try:
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(UserContext(user_id="ariel", current_stage="stage_1"))
            await session.commit()

        res = await client.post(
            "/api/advisor/turn",
            json={"user_id": "ariel", "last_user_message": ""},
        )
        assert res.status_code == 200, res.text

        async with db_mod.get_session() as session:
            rows = (
                await session.execute(
                    select(AgentReportRow).where(
                        AgentReportRow.user_id == "ariel",
                        AgentReportRow.agent_role == "advisor",
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].intake_session_id  # session id stamped
    finally:
        reset_advisor_agent_factory()


@pytest.mark.asyncio
async def test_advisor_turn_applies_context_updates(
    engine: None, client: AsyncClient
) -> None:
    """When the agent emits a context_update, the route must merge it
    into user_context.identity_yaml so the next turn sees it."""
    canned = _canned(
        "Got it.",
        context_updates=[
            {
                "target_section": "identity",
                "yaml_patch": "tax_residency: israel\n",
                "rationale": "User said they live in Tel Aviv.",
            }
        ],
    )
    set_advisor_agent_factory(_factory(canned))
    try:
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(UserContext(user_id="ariel", current_stage="stage_1"))
            await session.commit()

        res = await client.post(
            "/api/advisor/turn",
            json={"user_id": "ariel", "last_user_message": "I live in Tel Aviv."},
        )
        assert res.status_code == 200, res.text

        async with db_mod.get_session() as session:
            ctx = (
                await session.execute(
                    select(UserContext).where(UserContext.user_id == "ariel")
                )
            ).scalar_one()
            assert "israel" in (ctx.identity_yaml or "").lower()
    finally:
        reset_advisor_agent_factory()


# ----------------------------------------------------------------------
# /api/advisor/gaps
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advisor_gaps_empty_user_all_missing(
    engine: None, client: AsyncClient
) -> None:
    """A user with no user_context row at all should still yield a
    well-formed GapStatus where every catalog field is missing."""
    res = await client.get("/api/advisor/gaps", params={"user_id": "newbie"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["user_id"] == "newbie"
    assert body["counts"]["missing"] >= 10
    assert body["counts"]["fresh"] == 0
    assert body["counts"]["stale"] == 0


@pytest.mark.asyncio
async def test_advisor_gaps_marks_answered_fresh(
    engine: None, client: AsyncClient
) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(
            UserContext(
                user_id="ariel",
                identity_yaml="tax_residency: israel\nuser_citizenship: [israel]\n",
                current_stage="stage_1",
            )
        )
        await session.commit()

    res = await client.get("/api/advisor/gaps", params={"user_id": "ariel"})
    assert res.status_code == 200, res.text
    body = res.json()
    fresh_paths = {
        item["path"] for item in body["items"] if item["state"] == "fresh"
    }
    assert "identity.tax_residency" in fresh_paths
    assert "identity.user_citizenship" in fresh_paths


@pytest.mark.asyncio
async def test_advisor_gaps_items_carry_freshness_and_label(
    engine: None, client: AsyncClient
) -> None:
    res = await client.get("/api/advisor/gaps", params={"user_id": "freshie"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["items"], "expected at least one item"
    sample = body["items"][0]
    for k in ("path", "label", "section", "freshness", "priority", "state"):
        assert k in sample


# ----------------------------------------------------------------------
# Backwards-compat: legacy /api/intake/* aliases must keep working.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intake_alias_status_still_works(
    engine: None, client: AsyncClient
) -> None:
    res = await client.get("/api/intake/status", params={"user_id": "ariel"})
    assert res.status_code == 200
    assert res.json()["current_stage"] == "stage_1"


@pytest.mark.asyncio
async def test_intake_alias_turn_still_works(
    engine: None, client: AsyncClient
) -> None:
    """The legacy /api/intake/turn route now delegates persistence to
    the shared `_persist_turn` helper. It must continue to:
      - resolve current_stage via user_context
      - return TurnResponse (no `mode` field)
      - stamp an agent_reports row with role=intake (NOT advisor)
    """
    from argosy.agents.intake import IntakeAgent
    from argosy.api.routes.intake import (
        reset_intake_agent_factory,
        set_intake_agent_factory,
    )

    def _intake_factory(user_id: str):
        class _M(IntakeAgent):
            async def _call_model(self, *, system: str, user: str) -> ModelCall:
                return ModelCall(
                    text=json.dumps(
                        {
                            "stage": "stage_1",
                            "question_for_user": "tax residence?",
                            "context_updates": [],
                            "stage_complete": False,
                            "next_stage": None,
                            "confidence": "MEDIUM",
                            "cited_sources": [],
                            "notes_for_orchestrator": "",
                        }
                    ),
                    tokens_in=80,
                    tokens_out=80,
                    model=self.model,
                )

        return _M(user_id=user_id)

    set_intake_agent_factory(_intake_factory)
    try:
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(UserContext(user_id="ariel", current_stage="stage_1"))
            await session.commit()

        res = await client.post(
            "/api/intake/turn",
            json={"user_id": "ariel", "last_user_message": ""},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        # Legacy shape — no `mode` field on the response.
        assert "mode" not in body
        assert body["stage"] == "stage_1"

        # Audit log: agent_role must remain "intake" so cost rollups by
        # role still group the right turns together.
        async with db_mod.get_session() as session:
            rows = (
                await session.execute(
                    select(AgentReportRow).where(
                        AgentReportRow.user_id == "ariel",
                        AgentReportRow.agent_role == "intake",
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
    finally:
        reset_intake_agent_factory()
