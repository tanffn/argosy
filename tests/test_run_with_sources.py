"""BaseAgent.run accepts (system, user, sources) 3-tuple from build_prompt."""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from argosy.agents.base import BaseAgent


class _SourceConsumer(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})

    def build_prompt(self, **_):
        return (
            BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news",
            "Summarize the headlines.",
            [("news/2026-05-22.md", "Headline: NVDA up 3%.")],
        )


@pytest.mark.asyncio
async def test_sources_become_document_blocks(monkeypatch):
    agent = _SourceConsumer(user_id="ariel")
    fake_client = MagicMock()
    mock_msg = MagicMock()
    text_block = MagicMock(); text_block.type = "text"; text_block.text = "ok"
    mock_msg.content = [text_block]
    mock_msg.usage.input_tokens = 50; mock_msg.usage.output_tokens = 10
    mock_msg.usage.cache_read_input_tokens = 0
    mock_msg.usage.cache_creation_input_tokens = 0
    mock_msg.usage.thinking_tokens = 0
    mock_msg.model = "claude-sonnet-4-6"
    fake_client.messages.create.return_value = mock_msg
    agent._client = fake_client

    await agent._call_via_api_key(
        system=BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news",
        user="Summarize.",
        sources=[("news/2026-05-22.md", "Headline: NVDA up 3%.")],
    )

    call_kwargs = fake_client.messages.create.call_args.kwargs
    msgs = call_kwargs["messages"]
    # User message should now be a content-block list with the document block prepended
    user_content = msgs[0]["content"]
    assert isinstance(user_content, list)
    doc_blocks = [b for b in user_content if b.get("type") == "document"]
    assert len(doc_blocks) == 1
    assert doc_blocks[0]["title"] == "news/2026-05-22.md"
    assert doc_blocks[0]["citations"] == {"enabled": True}
