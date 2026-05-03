"""Argosy agent fleet (Phase 2: + news/macro/concentration analysts).

The public abstraction the rest of Argosy code touches is `BaseAgent`.
Phase 1 wraps the Anthropic Python SDK directly; later phases may switch
to the Claude Agent SDK without touching call sites that only use
`BaseAgent.run(...)`.
"""

from __future__ import annotations

from argosy.agents.base import AgentReport, BaseAgent, ConfidenceBand
from argosy.agents.concentration_analyst import (
    Breach,
    ConcentrationAnalystAgent,
    ConcentrationReport,
    NvdaPace,
)
from argosy.agents.errors import AgentRunError, MissingAPIKeyError
from argosy.agents.macro_analyst import MacroAnalystAgent, MacroReport
from argosy.agents.news_analyst import Headline, NewsAnalystAgent, NewsDigest

__all__ = [
    "AgentReport",
    "BaseAgent",
    "ConfidenceBand",
    "AgentRunError",
    "MissingAPIKeyError",
    "Breach",
    "ConcentrationAnalystAgent",
    "ConcentrationReport",
    "Headline",
    "MacroAnalystAgent",
    "MacroReport",
    "NewsAnalystAgent",
    "NewsDigest",
    "NvdaPace",
]
