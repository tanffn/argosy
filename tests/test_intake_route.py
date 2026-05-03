"""Intake API route tests."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from argosy.agents.base import ModelCall
from argosy.agents.intake import IntakeAgent
from argosy.api.routes.intake import (
    reset_intake_agent_factory,
    set_intake_agent_factory,
)
from argosy.state import db as db_mod
from argosy.state.models import User, UserContext


_CANNED = {
    "stage": "stage_1",
    "question_for_user": "What is your country of tax residence?",
    "context_updates": [],
    "stage_complete": False,
    "next_stage": None,
    "confidence": "MEDIUM",
    "cited_sources": [],
    "notes_for_orchestrator": "",
}


def _factory(user_id: str):
    class _M(IntakeAgent):
        async def _call_model(self, *, system: str, user: str) -> ModelCall:
            return ModelCall(
                text=json.dumps(_CANNED),
                tokens_in=80,
                tokens_out=120,
                model=self.model,
            )
    return _M(user_id=user_id)


@pytest.mark.asyncio
async def test_intake_status_default_stage_1(engine: None, client: AsyncClient) -> None:
    res = await client.get("/api/intake/status", params={"user_id": "ariel"})
    assert res.status_code == 200
    body = res.json()
    assert body["current_stage"] == "stage_1"


@pytest.mark.asyncio
async def test_intake_turn_returns_question(engine: None, client: AsyncClient) -> None:
    set_intake_agent_factory(_factory)
    try:
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(UserContext(user_id="ariel", current_stage="stage_1"))
            await session.commit()

        res = await client.post(
            "/api/intake/turn",
            json={"user_id": "ariel", "last_user_message": ""},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["stage"] == "stage_1"
        assert "tax residence" in body["question_for_user"]
        assert body["stage_complete"] is False
    finally:
        reset_intake_agent_factory()
