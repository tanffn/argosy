"""Shared chat contracts; transport identity is never supplied by the LLM."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ExecutionPolicy(StrEnum):
    NORMAL = "normal"
    ANALYSIS_ONLY = "analysis_only"


@dataclass(frozen=True)
class Principal:
    household_user_id: str
    guild_id: str
    channel_id: str
    user_id: str
    thread_id: str | None = None
    provider: str = "discord"


@dataclass(frozen=True)
class Citation:
    record_type: str
    record_id: str
    as_of: str
    version: str | None = None
    url: str | None = None
    label: str | None = None
    category: str | None = None


@dataclass
class ReadResult:
    topic: str
    data: Any
    citations: list[Citation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class AnalysisRequest:
    id: str
    instruments: list[str]
    state: str
    run_ids: list[int] = field(default_factory=list)
    error: str | None = None
    prompt_text: str = ""


@dataclass
class RunProgress:
    request_id: str
    state: str
    run_ids: list[int] = field(default_factory=list)
    agents: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class ChatAnswer:
    text: str
    citations: list[Citation] = field(default_factory=list)
    analysis_request_id: str | None = None


@dataclass
class OutboxEvent:
    semantic_key: str
    material_version: str
    category: str
    body: str
    citations: list[Citation] = field(default_factory=list)
    bypasses_quiet_hours: bool = False
    supersedes_message_id: str | None = None
    action_id: str | None = None
    expires_at: datetime | None = None
