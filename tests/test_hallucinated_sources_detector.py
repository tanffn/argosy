"""`_detect_hallucinated_sources` — grounded-citation false-positive fixes.

verify-run finding (2026-07-06): the detector compared citations against
supplied source_ids ONLY, so (a) a model citing the full article URL that
sits INSIDE a payload body (supplied id: ``news/ELF``) was flagged. Invented
non-URL ids must STILL be flagged.

Stream-B fix iteration 1: WebSearch *capability* alone must NOT ground an
unknown URL — only URLs observed in the tool-use record for that invocation
(or present in supplied source content) are exempt. The prior blanket
``claude_code_allowed_tools`` exemption laundered fabricated citations.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from argosy.agents.base import BaseAgent


class _Out(BaseModel):
    cited_sources: list[str] = []


class _Agent(BaseAgent[_Out]):
    agent_role = "test_halluc_detector"
    output_model = _Out
    require_citations = False

    def build_prompt(self, **inputs: Any) -> tuple[str, str]:
        return ("system", "user")


class _WebAgent(_Agent):
    agent_role = "test_halluc_detector_web"
    claude_code_allowed_tools = ("WebSearch",)


SOURCES = [
    ("news/ELF", "headline: tariffs hit ELF\nurl: https://example.com/elf-tariffs\nsummary: ..."),
    ("fundamentals/NVDA", "pe_ratio: 60.0"),
]


def test_supplied_id_passes() -> None:
    agent = _Agent(user_id="t")
    out = _Out(cited_sources=["news/ELF"])
    assert agent._detect_hallucinated_sources(out, SOURCES) == []


def test_invented_id_still_flagged() -> None:
    agent = _Agent(user_id="t")
    out = _Out(cited_sources=["robotaxi/FSD/Optimus"])
    assert agent._detect_hallucinated_sources(out, SOURCES) == [
        "robotaxi/FSD/Optimus"
    ]


def test_url_inside_source_content_is_grounded() -> None:
    """The ``news/ELF`` payload body carries the article URL; citing that
    URL is grounded, not hallucinated."""
    agent = _Agent(user_id="t")
    out = _Out(cited_sources=["https://example.com/elf-tariffs"])
    assert agent._detect_hallucinated_sources(out, SOURCES) == []


def test_unknown_url_flagged_without_web_access() -> None:
    agent = _Agent(user_id="t")
    out = _Out(cited_sources=["https://nowhere.example/made-up"])
    assert agent._detect_hallucinated_sources(out, SOURCES) == [
        "https://nowhere.example/made-up"
    ]


def test_unknown_url_flagged_even_with_web_search_capability() -> None:
    """WebSearch in the allowlist is NOT enough — no tool-use record ⇒ hallucinated."""
    agent = _WebAgent(user_id="t")
    out = _Out(cited_sources=["https://reuters.com/some-live-story"])
    assert agent._detect_hallucinated_sources(out, SOURCES) == [
        "https://reuters.com/some-live-story"
    ]


def test_unknown_url_grounded_when_in_tool_retrieved_urls() -> None:
    """A URL observed in this invocation's tool results is grounded."""
    agent = _WebAgent(user_id="t")
    out = _Out(cited_sources=["https://reuters.com/some-live-story"])
    assert agent._detect_hallucinated_sources(
        out,
        SOURCES,
        tool_retrieved_urls=["https://reuters.com/some-live-story"],
    ) == []


def test_invented_non_url_id_flagged_even_with_web_search() -> None:
    """WebSearch access only excuses URL-form citations that were retrieved —
    invented ids stay flagged."""
    agent = _WebAgent(user_id="t")
    out = _Out(cited_sources=["robotaxi/FSD/Optimus"])
    assert agent._detect_hallucinated_sources(
        out,
        SOURCES,
        tool_retrieved_urls=["https://reuters.com/some-live-story"],
    ) == ["robotaxi/FSD/Optimus"]
