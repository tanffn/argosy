"""Durable, tenant-scoped records for private chat transports."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from argosy.state.models import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ChatBinding(Base):
    __tablename__ = "chat_bindings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    household_user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    read_scope: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "guild_id",
            "channel_id",
            "provider_user_id",
            name="uq_chat_binding_identity",
        ),
        Index("ix_chat_bindings_household", "household_user_id", "enabled"),
    )


class ChatThread(Base):
    __tablename__ = "chat_threads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    binding_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_bindings.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_provider_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    recommendation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recommendation_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    __table_args__ = (UniqueConstraint("binding_id", "thread_id", name="uq_chat_thread_binding"),)


class ChatTurn(Base):
    __tablename__ = "chat_turns"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    binding_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_bindings.id", ondelete="CASCADE"), nullable=False
    )
    household_user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_key: Mapped[str] = mapped_column(String(96), nullable=False)
    inbound_message_id: Mapped[str] = mapped_column(String(32), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_nonce: Mapped[str | None] = mapped_column(String(32), nullable=True)
    response_message_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    citations_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    __table_args__ = (
        UniqueConstraint("binding_id", "inbound_message_id", name="uq_chat_turn_inbound"),
        UniqueConstraint("response_nonce", name="uq_chat_turn_response_nonce"),
        Index("ix_chat_turns_conversation", "household_user_id", "conversation_key", "created_at"),
    )


class ChatAnalysisRequest(Base):
    __tablename__ = "chat_analysis_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    binding_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_bindings.id", ondelete="CASCADE"), nullable=False
    )
    household_user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    inbound_message_id: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    instruments_json: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    run_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    progress_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    progress_nonce: Mapped[str | None] = mapped_column(String(32), nullable=True)
    progress_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_of_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("chat_analysis_requests.id", ondelete="SET NULL"), nullable=True
    )
    final_nonce: Mapped[str | None] = mapped_column(String(32), nullable=True)
    final_message_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    __table_args__ = (
        UniqueConstraint("binding_id", "inbound_message_id", name="uq_chat_analysis_inbound"),
        Index("ix_chat_analysis_active", "household_user_id", "status", "created_at"),
    )


class ChatAgentProgress(Base):
    __tablename__ = "chat_agent_progress"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chat_analysis_requests.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("decision_runs.id", ondelete="SET NULL"), nullable=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False)
    agent: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    __table_args__ = (
        UniqueConstraint("request_id", "sequence", name="uq_chat_progress_sequence"),
        UniqueConstraint("request_id", "dedup_key", name="uq_chat_progress_dedup"),
    )


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    binding_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_bindings.id", ondelete="CASCADE"), nullable=False
    )
    household_user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    semantic_key: Mapped[str] = mapped_column(String(256), nullable=False)
    material_version: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    citations_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    nonce: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    supersedes_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    __table_args__ = (
        UniqueConstraint(
            "binding_id", "semantic_key", "material_version", name="uq_notification_material"
        ),
        UniqueConstraint("nonce", name="uq_notification_nonce"),
        Index("ix_notification_delivery", "household_user_id", "status", "retry_after"),
    )


class ChatCursor(Base):
    __tablename__ = "chat_cursors"
    binding_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_bindings.id", ondelete="CASCADE"), primary_key=True
    )
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    last_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gateway_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    gateway_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


__all__ = [
    "ChatBinding",
    "ChatThread",
    "ChatTurn",
    "ChatAnalysisRequest",
    "ChatAgentProgress",
    "NotificationOutbox",
    "ChatCursor",
]
