"""Argosy agent fleet (Phase 1: intake + plan-critique).

The public abstraction the rest of Argosy code touches is `BaseAgent`.
Phase 1 wraps the Anthropic Python SDK directly; later phases may switch
to the Claude Agent SDK without touching call sites that only use
`BaseAgent.run(...)`.
"""

from __future__ import annotations

from argosy.agents.base import AgentReport, BaseAgent, ConfidenceBand
from argosy.agents.errors import AgentRunError, MissingAPIKeyError

__all__ = [
    "AgentReport",
    "BaseAgent",
    "ConfidenceBand",
    "AgentRunError",
    "MissingAPIKeyError",
]
