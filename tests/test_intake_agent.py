"""IntakeAgent tests. Mock the Anthropic client so no network call happens."""

from __future__ import annotations

import json

import pytest

from argosy.agents.base import ConfidenceBand, ModelCall
from argosy.agents.intake import (
    INTAKE_STAGES,
    ContextUpdate,
    IntakeAgent,
    IntakeTurnOutput,
)


class _MockIntakeAgent(IntakeAgent):
    """Replaces `_call_model` with a canned `ModelCall`."""

    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output

    async def _call_model(self, *, system: str, user: str) -> ModelCall:
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=120,
            tokens_out=80,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_intake_basic_turn_returns_question() -> None:
    canned = {
        "stage": "stage_1",
        "question_for_user": "What is your country of tax residence?",
        "context_updates": [],
        "stage_complete": False,
        "next_stage": None,
        "confidence": "MEDIUM",
        "cited_sources": [],
        "notes_for_orchestrator": "",
    }
    agent = _MockIntakeAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(current_stage="stage_1", accumulated_context="", last_user_message="")
    out = report.output
    assert isinstance(out, IntakeTurnOutput)
    assert out.question_for_user.startswith("What is your country")
    assert out.stage == "stage_1"
    assert report.confidence == ConfidenceBand.MEDIUM
    assert report.tokens_in == 120
    assert report.tokens_out == 80
    assert report.cost_usd > 0


@pytest.mark.asyncio
async def test_intake_stage_complete_signals_advancement() -> None:
    canned = {
        "stage": "stage_1",
        "question_for_user": "",
        "context_updates": [
            {
                "target_section": "identity",
                "yaml_patch": "tax_residency: israel\ncitizenship: [israel]\n",
                "rationale": "User confirmed Israeli residency.",
            }
        ],
        "stage_complete": True,
        "next_stage": "stage_2",
        "confidence": "HIGH",
        "cited_sources": [],
        "notes_for_orchestrator": "",
    }
    agent = _MockIntakeAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        current_stage="stage_1",
        accumulated_context="",
        last_user_message="I am an Israeli tax resident.",
    )
    out = report.output
    assert out.stage_complete is True
    assert out.next_stage == "stage_2"
    assert len(out.context_updates) == 1
    upd = out.context_updates[0]
    assert isinstance(upd, ContextUpdate)
    assert upd.target_section == "identity"
    assert "israel" in upd.yaml_patch.lower()


def test_intake_stages_are_six() -> None:
    assert len(INTAKE_STAGES) == 6
    assert INTAKE_STAGES[0] == "stage_1"
    assert INTAKE_STAGES[-1] == "stage_6"


@pytest.mark.asyncio
async def test_intake_unknown_stage_raises() -> None:
    canned = {
        "stage": "stage_1",
        "question_for_user": "x",
        "context_updates": [],
        "stage_complete": False,
        "next_stage": None,
        "confidence": "LOW",
        "cited_sources": [],
        "notes_for_orchestrator": "",
    }
    agent = _MockIntakeAgent(user_id="ariel", canned_output=canned)
    with pytest.raises(ValueError):
        await agent.run(
            current_stage="stage_99",
            accumulated_context="",
            last_user_message="",
        )
