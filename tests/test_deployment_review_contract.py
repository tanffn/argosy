"""Malformed reviewer output must be repaired, never invented as a judgment."""
import json

import pytest
from pydantic import ValidationError

from argosy.agents.base import ModelCall
from argosy.agents.deployment_reviewer import DeploymentReviewerAgent, DeploymentReviewOutput
from argosy.agents.errors import AgentRunError


@pytest.mark.parametrize("value", [None, "true", "false", 0, 1])
def test_separation_requires_an_explicit_json_boolean(value):
    with pytest.raises(ValidationError):
        DeploymentReviewOutput(lens="sizing", separation_safe=value)


def test_approval_in_prose_cannot_substitute_for_a_missing_field():
    with pytest.raises(ValidationError):
        DeploymentReviewOutput(lens="sizing", overall_note="separation_safe=true")


@pytest.mark.parametrize("decision", [True, False])
async def test_reviewer_repairs_omitted_field_without_choosing_the_judgment(monkeypatch, decision):
    agent = DeploymentReviewerAgent(user_id="ariel")
    calls = []

    async def model(**kwargs):
        calls.append(kwargs)
        output = {"lens": "sizing", "objections": [], "overall_note": "Independent review"}
        if len(calls) == 2:
            output["separation_safe"] = decision
        return ModelCall(text=json.dumps(output), tokens_in=1, tokens_out=1, model="test")

    monkeypatch.setattr(agent, "_call_model", model)
    result = await agent.run(lens="sizing", packet={}, buys=[])
    assert result.output.separation_safe is decision
    assert len(calls) == 2
    assert '"separation_safe": boolean' in calls[0]["user"]
    assert "validation_feedback" in calls[1]["user"]
    assert "separation_safe" in calls[1]["user"]


async def test_repeat_malformed_review_fails_after_one_repair(monkeypatch):
    agent = DeploymentReviewerAgent(user_id="ariel")
    calls = []

    async def model(**kwargs):
        calls.append(kwargs)
        return ModelCall(text='{"lens":"sizing","overall_note":"separation_safe=true"}',
                         tokens_in=1, tokens_out=1, model="test")

    monkeypatch.setattr(agent, "_call_model", model)
    with pytest.raises(AgentRunError):
        await agent.run(lens="sizing", packet={}, buys=[])
    assert len(calls) == 2
