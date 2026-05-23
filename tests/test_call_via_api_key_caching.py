"""_call_via_api_key passes system as content blocks with cache_control."""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from argosy.agents.base import BaseAgent


class _DummyAgent(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})

    def build_prompt(self, **_):
        return ("", "")


def _make_mock_msg(input_toks=100, output_toks=50, cache_read=0, cache_create=0):
    msg = MagicMock()
    msg.content = [MagicMock(text="ok", type="text")]
    msg.content[0].text = "ok"
    msg.usage.input_tokens = input_toks
    msg.usage.output_tokens = output_toks
    msg.usage.cache_read_input_tokens = cache_read
    msg.usage.cache_creation_input_tokens = cache_create
    msg.model = "claude-sonnet-4-6"
    return msg


@pytest.mark.asyncio
async def test_system_passed_as_content_blocks_with_cache_control(monkeypatch):
    agent = _DummyAgent(user_id="ariel")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_mock_msg(cache_create=80)
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news"
    result = await agent._call_via_api_key(system=full_system, user="hello")

    # Assert messages.create was called with system as a list of two blocks
    call_kwargs = fake_client.messages.create.call_args.kwargs
    system_blocks = call_kwargs["system"]
    assert isinstance(system_blocks, list)
    assert len(system_blocks) == 2
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}

    # Assert cache telemetry flowed back into the ModelCall
    assert result.cache_creation_tokens == 80
    assert result.cache_input_tokens == 0


@pytest.mark.asyncio
async def test_cache_read_telemetry_threaded_through(monkeypatch):
    agent = _DummyAgent(user_id="ariel")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_mock_msg(cache_read=200)
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news"
    result = await agent._call_via_api_key(system=full_system, user="hello")
    assert result.cache_input_tokens == 200
    assert result.cache_creation_tokens == 0
