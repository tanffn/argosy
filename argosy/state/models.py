"""SQLAlchemy models for Argosy.

Phase 0: `users` and `user_context`.
Phase 1: `plan_versions`, `plan_critiques`, `agent_reports`,
`agent_reports_blobs`. Adds `current_stage` to `user_context`.

Other table groups (holdings, decisions full lifecycle, audit beyond agent
reports, external caches, domain status, operations) come in later phases.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Argosy declarative base."""


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    context: Mapped["UserContext | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class UserContext(Base):
    __tablename__ = "user_context"

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # YAML payloads are stored as TEXT; structured access is done in app code.
    identity_yaml: Mapped[str] = mapped_column(Text, nullable=False, default="")
    goals_yaml: Mapped[str] = mapped_column(Text, nullable=False, default="")
    constraints_yaml: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Phase 1: tracks where the intake interview is in the 6-stage flow.
    # NULL = not started; "stage_1" .. "stage_6"; "complete" once all stages done.
    current_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="context")


class PlanVersion(Base):
    """An imported plan document (markdown) with a user-supplied label.

    A user typically has multiple versions over time (v1.0, v2.0, v2.1...).
    The plan-critique agent reads the latest version by default.
    """

    __tablename__ = "plan_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_label: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    raw_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    critiques: Mapped[list["PlanCritique"]] = relationship(
        back_populates="plan_version", cascade="all, delete-orphan"
    )


class PlanCritique(Base):
    """A plan-critique agent run output, stored as JSON in `critique_json`.

    `critique_json` conforms to `argosy.agents.plan_critique.PlanCritiqueReport`.
    """

    __tablename__ = "plan_critiques"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    critique_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    plan_version: Mapped[PlanVersion] = relationship(back_populates="critiques")


class AgentReport(Base):
    """One row per agent invocation. Append-only audit log.

    `response_text` carries the raw model response (or a structured serialization
    of it). Tokens and cost stamped per invocation for monthly cost rollups.
    `decision_id` is nullable: cross-cutting agents (intake, plan-critique
    standalone, domain-refresh) have no enclosing decision.
    """

    __tablename__ = "agent_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_role: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    response_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    blobs: Mapped[list["AgentReportBlob"]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )


class AgentReportBlob(Base):
    """Key/value side data for an agent report (e.g., 'inputs_json', 'tools_used').

    Avoids ballooning `agent_reports.response_text` with multi-MB attachments.
    """

    __tablename__ = "agent_reports_blobs"

    report_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_reports.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")

    report: Mapped[AgentReport] = relationship(back_populates="blobs")


__all__ = [
    "Base",
    "User",
    "UserContext",
    "PlanVersion",
    "PlanCritique",
    "AgentReport",
    "AgentReportBlob",
]
