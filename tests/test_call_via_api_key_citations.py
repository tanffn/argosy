"""_call_via_api_key extracts citations from response content blocks."""
from __future__ import annotations
import json
from unittest.mock import MagicMock

import pytest

from argosy.agents.base import BaseAgent


class _News(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})
    def build_prompt(self, **_): return ("", "", [])


def _mock_msg_with_citations():
    msg = MagicMock()
    # First content block: text with citations metadata
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "The CGT rate is 25%."
    citation = MagicMock()
    citation.type = "char_location"
    citation.cited_text = "capital gains tax rate for individuals is 25%"
    citation.document_index = 0
    citation.document_title = "domain_knowledge/tax/israel/capital_gains.md"
    citation.start_char_index = 1240
    citation.end_char_index = 1389
    text_block.citations = [citation]
    msg.content = [text_block]
    msg.usage.input_tokens = 100; msg.usage.output_tokens = 20
    msg.usage.cache_read_input_tokens = 0
    msg.usage.cache_creation_input_tokens = 0
    msg.usage.thinking_tokens = 0
    msg.model = "claude-sonnet-4-6"
    return msg


@pytest.mark.asyncio
async def test_citations_extracted_to_json(monkeypatch):
    agent = _News(user_id="ariel")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_msg_with_citations()
    agent._client = fake_client

    full_system = BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news"
    result = await agent._call_via_api_key(
        system=full_system, user="What's the CGT rate?",
        sources=[("domain_knowledge/tax/israel/capital_gains.md", "..." * 500)],
    )

    assert result.citations_json is not None
    parsed = json.loads(result.citations_json)
    assert len(parsed) == 1
    c = parsed[0]
    assert c["source_id"] == "domain_knowledge/tax/israel/capital_gains.md"
    assert c["source_span_start"] == 1240
    assert c["source_span_end"] == 1389
    assert c["claim_text"] == "The CGT rate is 25%."
    assert c["cited_quote"] == "capital gains tax rate for individuals is 25%"


@pytest.mark.asyncio
async def test_no_citations_returns_null(monkeypatch):
    """Response without any citation blocks: citations_json stays None."""
    agent = _News(user_id="ariel")
    fake_client = MagicMock()
    mock_msg = MagicMock()
    text_block = MagicMock(); text_block.type = "text"; text_block.text = "ok"; text_block.citations = []
    mock_msg.content = [text_block]
    mock_msg.usage.input_tokens = 10; mock_msg.usage.output_tokens = 5
    mock_msg.usage.cache_read_input_tokens = 0
    mock_msg.usage.cache_creation_input_tokens = 0
    mock_msg.usage.thinking_tokens = 0
    mock_msg.model = "claude-sonnet-4-6"
    fake_client.messages.create.return_value = mock_msg
    agent._client = fake_client

    result = await agent._call_via_api_key(
        system=BaseAgent.BOILERPLATE_SYSTEM + "\n\nRole: news",
        user="hello",
    )
    assert result.citations_json is None
