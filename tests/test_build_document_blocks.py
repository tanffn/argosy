"""_build_document_blocks converts a list of (source_id, content) tuples into
Anthropic document content blocks with citations enabled."""
from __future__ import annotations

from argosy.agents.base import BaseAgent


class _News(BaseAgent):
    agent_role = "news_analyst"
    output_model = type("Out", (), {})
    def build_prompt(self, **_): return ("", "")


def test_empty_returns_empty_list():
    agent = _News(user_id="ariel")
    assert agent._build_document_blocks([]) == []


def test_single_source():
    agent = _News(user_id="ariel")
    blocks = agent._build_document_blocks([
        ("domain_knowledge/tax/israel/capital_gains.md", "The CGT rate is 25% for individuals."),
    ])
    assert len(blocks) == 1
    b = blocks[0]
    assert b["type"] == "document"
    assert b["source"]["type"] == "text"
    assert b["source"]["media_type"] == "text/plain"
    assert b["source"]["data"] == "The CGT rate is 25% for individuals."
    assert b["title"] == "domain_knowledge/tax/israel/capital_gains.md"
    assert b["citations"] == {"enabled": True}


def test_multiple_sources_preserves_order():
    agent = _News(user_id="ariel")
    blocks = agent._build_document_blocks([
        ("source_a.md", "Content A"),
        ("source_b.md", "Content B"),
    ])
    assert len(blocks) == 2
    assert blocks[0]["title"] == "source_a.md"
    assert blocks[1]["title"] == "source_b.md"
