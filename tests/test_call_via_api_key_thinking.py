"""_call_via_api_key passes thinking param when budget > 0 and extracts thinking_tokens."""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from argosy.agents.base import BaseAgent


class _Trader(BaseAgent):
    agent_role = "trader"
    output_model = type("Out", (), {})
    def build_prompt(self, **_): return ("", "")


class _News(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})
    def build_prompt(self, **_): return ("", "")


def _make_mock_msg(input_toks=100, output_toks=50, thinking_toks=0):
    msg = MagicMock()
    blocks = []
    if thinking_toks:
        thinking_block = MagicMock(spec=["type", "thinking"])
        thinking_block.type = "thinking"
        thinking_block.thinking = "thinking text"
        # Real ThinkingBlock has no `.text`; getattr(..., None) returns None.
        # spec=[...] above ensures MagicMock raises AttributeError for unspecced
        # attrs so getattr defaults kick in correctly.
        blocks.append(thinking_block)
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "ok"
    blocks.append(text_block)
    msg.content = blocks
    msg.usage.input_tokens = input_toks
    msg.usage.output_tokens = output_toks
    msg.usage.cache_read_input_tokens = 0
    msg.usage.cache_creation_input_tokens = 0
    # Anthropic puts thinking tokens in a separate counter:
    msg.usage.cache_creation = MagicMock()
    msg.model = "claude-opus-4-7"
    return msg


@pytest.mark.asyncio
async def test_thinking_passed_when_budget_positive(monkeypatch):
    agent = _Trader(user_id="ariel")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_mock_msg(thinking_toks=500)
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: trader"
    await agent._call_via_api_key(system=full_system, user="hello")

    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert "thinking" in call_kwargs
    assert call_kwargs["thinking"] == {"type": "enabled", "budget_tokens": 8000}


@pytest.mark.asyncio
async def test_thinking_NOT_passed_when_budget_zero(monkeypatch):
    agent = _News(user_id="ariel")  # news_analyst has budget=0
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_mock_msg()
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news"
    await agent._call_via_api_key(system=full_system, user="hello")

    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert "thinking" not in call_kwargs
