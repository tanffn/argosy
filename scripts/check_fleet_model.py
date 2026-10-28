"""Live model smoke through Argosy's real BaseAgent transport, no trades.

Run: .venv/Scripts/python.exe scripts/check_fleet_model.py
Verifies provider-reported identity, not the model's self-identification.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import claude_agent_sdk
from pydantic import BaseModel

from argosy.agents.base import BaseAgent


class SmokeOutput(BaseModel):
    result: str


class ModelSmokeAgent(BaseAgent[SmokeOutput]):
    agent_role = "model_smoke"
    output_model = SmokeOutput
    require_citations = False
    use_structured_output = True
    claude_code_force_toolless = True

    def build_prompt(self, **inputs):
        return (
            'Return only JSON matching {"result": "ok"}. This is a transport check.',
            'Return {"result": "ok"}.',
        )


async def main():
    observed_models = set()
    original_query = claude_agent_sdk.query

    async def observed_query(*args, **kwargs):
        async for message in original_query(*args, **kwargs):
            if isinstance(message, claude_agent_sdk.AssistantMessage):
                observed_models.add(message.model)
            yield message

    claude_agent_sdk.query = observed_query
    try:
        agent = ModelSmokeAgent(user_id="ariel")
        agent.thinking_effort = "low"
        agent.sdk_timeout_seconds = 120
        report = await agent.run(decision_id="opus-5-5-transport-smoke")
        result = {
            "requested_model": agent.model,
            "provider_models": sorted(observed_models),
            "output": report.output.model_dump(),
        }
        print(json.dumps(result, default=str))
        assert observed_models == {"claude-opus-5-5"}, result
        assert report.output.result == "ok", result
    finally:
        claude_agent_sdk.query = original_query


if __name__ == "__main__":
    asyncio.run(main())
