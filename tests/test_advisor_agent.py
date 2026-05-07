"""AdvisorAgent tests. Mock the Anthropic client so no network call happens."""

from __future__ import annotations

import json

import pytest

from argosy.agents.advisor import AdvisorAgent, AdvisorTurnOutput
from argosy.agents.base import ConfidenceBand, ModelCall


class _MockAdvisor(AdvisorAgent):
    """Replaces `_call_model` with a canned `ModelCall`."""

    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output

    async def _call_model(self, *, system: str, user: str) -> ModelCall:
        # Stash the prompts on the instance so the tests can inspect.
        self._last_system = system
        self._last_user = user
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=140,
            tokens_out=90,
            model=self.model,
        )


_BASE_CANNED = {
    "stage": "stage_1",
    "question_for_user": "What is your country of tax residence?",
    "context_updates": [],
    "stage_complete": False,
    "next_stage": None,
    "confidence": "MEDIUM",
    "cited_sources": [],
    "notes_for_orchestrator": "",
    "mode": "gap_driven",
}


@pytest.mark.asyncio
async def test_advisor_gap_driven_emits_question() -> None:
    agent = _MockAdvisor(user_id="ariel", canned_output=_BASE_CANNED)
    report = await agent.run(
        current_stage="stage_1",
        accumulated_context="",
        last_user_message="",
        mode="gap_driven",
        answered_fields=[],
        missing_fields=[
            "identity.tax_residency",
            "identity.user_citizenship",
        ],
    )
    out = report.output
    assert isinstance(out, AdvisorTurnOutput)
    assert out.mode == "gap_driven"
    assert "tax residence" in out.question_for_user.lower()
    assert report.confidence == ConfidenceBand.MEDIUM
    assert report.agent_role == "advisor"

    # Prompt sanity: the gap-driven mode addendum must be injected.
    assert "MODE: gap_driven" in agent._last_system
    # And the answered/missing lists must be present.
    assert "ALREADY ANSWERED" in agent._last_user
    assert "STILL NEEDED" in agent._last_user
    assert "identity.tax_residency" in agent._last_user


@pytest.mark.asyncio
async def test_advisor_user_driven_addendum_in_prompt() -> None:
    canned = {**_BASE_CANNED, "mode": "user_driven"}
    agent = _MockAdvisor(user_id="ariel", canned_output=canned)
    report = await agent.run(
        current_stage="stage_3",
        accumulated_context="tax_residency: israel\n",
        last_user_message="What's the kupat-gemel ceiling for 2026?",
        mode="user_driven",
        answered_fields=["identity.tax_residency"],
        missing_fields=["identity.user_employment_gross_annual"],
    )
    out = report.output
    assert out.mode == "user_driven"

    # The user-driven addendum must steer the agent to ANSWER + LOG +
    # follow-up rather than to interrogate.
    assert "MODE: user_driven" in agent._last_system
    assert "ANSWER the question" in agent._last_system
    # The user's message must be embedded so the model can see it.
    assert "kupat-gemel" in agent._last_user


@pytest.mark.asyncio
async def test_advisor_target_field_focus_hint_renders() -> None:
    """Clicking a sidebar row routes through `target_field`; the prompt
    must surface a FOCUS HINT for that field."""
    agent = _MockAdvisor(user_id="ariel", canned_output=_BASE_CANNED)
    await agent.run(
        current_stage="stage_3",
        accumulated_context="",
        last_user_message="",
        mode="gap_driven",
        answered_fields=[],
        missing_fields=["identity.bank_accounts", "identity.brokerage_accounts"],
        target_field="identity.bank_accounts",
    )
    assert "FOCUS HINT" in agent._last_user
    assert "identity.bank_accounts" in agent._last_user


@pytest.mark.asyncio
async def test_advisor_target_field_ignored_in_user_driven_mode() -> None:
    """In user_driven mode the target_field hint is suppressed — the user
    is steering the conversation, not the gap tracker."""
    canned = {**_BASE_CANNED, "mode": "user_driven"}
    agent = _MockAdvisor(user_id="ariel", canned_output=canned)
    await agent.run(
        current_stage="stage_1",
        accumulated_context="",
        last_user_message="Should I open an IBKR account?",
        mode="user_driven",
        answered_fields=[],
        missing_fields=["identity.tax_residency"],
        target_field="identity.bank_accounts",
    )
    assert "FOCUS HINT" not in agent._last_user


@pytest.mark.asyncio
async def test_advisor_unknown_stage_raises() -> None:
    agent = _MockAdvisor(user_id="ariel", canned_output=_BASE_CANNED)
    with pytest.raises(ValueError):
        await agent.run(
            current_stage="stage_99",
            accumulated_context="",
            last_user_message="",
            mode="gap_driven",
        )


def test_advisor_output_inherits_intake_shape() -> None:
    """AdvisorTurnOutput must remain a strict superset of IntakeTurnOutput
    so legacy callers reading the fields they know about don't break."""
    from argosy.agents.intake import IntakeTurnOutput

    intake_fields = set(IntakeTurnOutput.model_fields.keys())
    advisor_fields = set(AdvisorTurnOutput.model_fields.keys())
    assert intake_fields.issubset(advisor_fields)
    assert "mode" in advisor_fields - intake_fields


# --- Wave 4 Task 4.3: amendment field + amendment classification prompt block ---


def test_advisor_turn_carries_optional_amendment_field() -> None:
    """AdvisorTurnOutput now has an optional `amendment: AmendmentIntent | None`
    field so the advisor can emit a plan-amendment classification inline with
    its turn output. The field must round-trip through JSON cleanly."""
    from argosy.agents.advisor_amendment_types import AmendmentIntent

    turn = AdvisorTurnOutput(
        stage="stage_1",
        question_for_user="I'll apply this as a small tightening.",
        context_updates=[],
        stage_complete=False,
        next_stage=None,
        confidence="MEDIUM",
        cited_sources=[],
        notes_for_orchestrator="",
        mode="user_driven",
        amendment=AmendmentIntent(
            tier="small",
            direction="tighten",
            rationale="single target, explicit numbers",
        ),
    )
    payload = turn.model_dump_json()
    turn2 = AdvisorTurnOutput.model_validate_json(payload)
    assert turn2.amendment is not None
    assert turn2.amendment.tier == "small"
    assert turn2.amendment.direction == "tighten"


def test_advisor_turn_amendment_optional_is_none_by_default() -> None:
    """If the model doesn't emit an amendment, the field defaults to None
    so legacy turns (intake gap-filling, plain Q&A) keep their old shape."""
    turn = AdvisorTurnOutput(
        stage="stage_1",
        question_for_user="just chatting",
        context_updates=[],
        stage_complete=False,
        next_stage=None,
        confidence="MEDIUM",
        cited_sources=[],
        notes_for_orchestrator="",
        mode="user_driven",
    )
    assert turn.amendment is None


def test_advisor_prompt_includes_amendment_classification_block() -> None:
    """When the user has a current plan (`has_current_plan=True`), the system
    prompt must include the AMENDMENT INTENT DETECTION instructions so the
    advisor knows to classify amendment requests into small/medium/large."""
    agent = AdvisorAgent(user_id="ariel")
    sys, _usr = agent.build_prompt(
        current_stage="stage_1",
        accumulated_context="",
        last_user_message="tighten NVDA cap to 12%",
        history_excerpt="",
        answered_fields=[],
        missing_fields=["identity.tax_residency"],
        mode="user_driven",
        has_current_plan=True,
    )
    assert "AMENDMENT INTENT DETECTION" in sys
    sys_lower = sys.lower()
    assert "small" in sys_lower
    assert "medium" in sys_lower
    assert "large" in sys_lower


def test_advisor_prompt_omits_amendment_block_without_current_plan() -> None:
    """If the user has no current plan, the amendment block is omitted —
    no point asking the model to classify amendments to a plan that
    doesn't exist yet."""
    agent = AdvisorAgent(user_id="ariel")
    sys, _usr = agent.build_prompt(
        current_stage="stage_1",
        accumulated_context="",
        last_user_message="hello",
        history_excerpt="",
        answered_fields=[],
        missing_fields=["identity.tax_residency"],
        mode="user_driven",
        has_current_plan=False,
    )
    assert "AMENDMENT INTENT DETECTION" not in sys
