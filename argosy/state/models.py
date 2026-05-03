"""SQLAlchemy models for Argosy.

Phase 0: `users` and `user_context`.
Phase 1: `plan_versions`, `plan_critiques`, `agent_reports`,
`agent_reports_blobs`. Adds `current_stage` to `user_context`.
Phase 2: `cadence_state`, `daily_briefs`, `prices_cache`, `news_cache`,
`macro_cache`.
Phase 3: `proposals`, `proposals_history`, `approvals`, `decision_runs`.

Other table groups (broker order log, lots, audit beyond agent reports,
domain status, operations) come in later phases.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
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


class CadenceState(Base):
    """Per-loop scheduler bookkeeping (Phase 2).

    Loops are global, not per-user — the scheduler is one process serving
    one ARGOSY_HOME, so we key by `loop_name`. When productizing, each
    tenant gets its own ARGOSY_HOME (or `loop_name` is namespaced).
    """

    __tablename__ = "cadence_state"

    loop_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_tick_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class DailyBrief(Base):
    """One Daily Brief run record. Holds the four analyst reports + summary.

    `news_report_json`, `macro_report_json`, `concentration_report_json`, and
    `plan_delta_json` are pydantic-validated payloads serialized to JSON.
    """

    __tablename__ = "daily_briefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
    summary_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    news_report_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    macro_report_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    concentration_report_json: Mapped[str] = mapped_column(
        Text, nullable=False, default=""
    )
    plan_delta_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class _CacheBase:
    """Shared columns for the three external-data caches.

    Composite PK (provider, key) — different providers can use the same key.
    `payload_json` is the raw JSON-serialized response; `payload_hash` is a
    sha256 of the payload for audit / change-detection.
    """

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class PricesCache(Base, _CacheBase):
    __tablename__ = "prices_cache"


class NewsCache(Base, _CacheBase):
    __tablename__ = "news_cache"


class MacroCache(Base, _CacheBase):
    __tablename__ = "macro_cache"


# ----------------------------------------------------------------------
# Phase 3: decisions + proposals
# ----------------------------------------------------------------------


class Proposal(Base):
    """One trader proposal. Lives across the SDD §10 state machine.

    Status strings come from `argosy.decisions.proposals.ProposalStatus`
    (kept as str here so migrations are simpler).
    """

    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    size_shares_or_currency: Mapped[float] = mapped_column(
        Numeric(18, 4), nullable=False, default=0
    )
    size_units: Mapped[str] = mapped_column(String(16), nullable=False, default="shares")
    instrument: Mapped[str] = mapped_column(String(16), nullable=False, default="stock")
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="market")
    limit_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    time_in_force: Mapped[str] = mapped_column(String(8), nullable=False, default="DAY")
    tier: Mapped[str] = mapped_column(String(4), nullable=False, index=True)
    account_class: Mapped[str] = mapped_column(String(16), nullable=False, default="main")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    rationale_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expected_impact_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cooling_off_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("decision_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    history: Mapped[list["ProposalHistory"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )
    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )


class ProposalHistory(Base):
    """Append-only state-machine audit. One row per status change."""

    __tablename__ = "proposals_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    transitioned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    transitioned_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    proposal: Mapped[Proposal] = relationship(back_populates="history")


class Approval(Base):
    """One row per approval action (dashboard click, email link, etc.)."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    approval_channel: Mapped[str] = mapped_column(String(32), nullable=False, default="dashboard")
    second_factor_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    signed_token_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    proposal: Mapped[Proposal] = relationship(back_populates="approvals")


class DecisionRun(Base):
    """One end-to-end decision-flow execution.

    Links a flow run to the agent_reports rows it produced and to the
    proposal it emitted (NULL until the trader fires).
    """

    __tablename__ = "decision_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    tier: Mapped[str] = mapped_column(String(4), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    fund_manager_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    proposal_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("proposals.id", ondelete="SET NULL"), nullable=True
    )


__all__ = [
    "Base",
    "User",
    "UserContext",
    "PlanVersion",
    "PlanCritique",
    "AgentReport",
    "AgentReportBlob",
    "CadenceState",
    "DailyBrief",
    "PricesCache",
    "NewsCache",
    "MacroCache",
    # Phase 3
    "Approval",
    "DecisionRun",
    "Proposal",
    "ProposalHistory",
]
