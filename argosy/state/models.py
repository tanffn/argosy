"""SQLAlchemy models for Argosy.

Phase 0: `users` and `user_context`.
Phase 1: `plan_versions`, `plan_critiques`, `agent_reports`,
`agent_reports_blobs`. Adds `current_stage` to `user_context`.
Phase 2: `cadence_state`, `daily_briefs`, `prices_cache`, `news_cache`,
`macro_cache`.
Phase 3: `proposals`, `proposals_history`, `approvals`, `decision_runs`.
Phase 4: `audit_log`, `lots`, `fills`, `pending_orders`. The audit_log
table is the universal event log per SDD §14.1; lots holds per-lot
cost-basis from broker imports; fills records each execution event;
pending_orders tracks open broker orders awaiting reconciliation.
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
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Argosy declarative base."""


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Phase 6: NextAuth JWT email claim -> user_id mapping. Nullable
    # because Phase 1-5 users predate this column.
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # Phase 6: cached entitlements plan ("free" | "pro" | "enterprise").
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
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


# ----------------------------------------------------------------------
# Phase 4: audit log + lots + fills + pending_orders
# ----------------------------------------------------------------------


class AuditLog(Base):
    """Universal append-only audit log (SDD §14.1).

    One row per recorded event: every fill, every approval, every
    override, every paper fill, every credential use. `event_type` is a
    free-form string namespace ("fill.received", "approval.granted",
    "paper_fill.recorded", "credential.used", ...). `entity_type` /
    `entity_id` link the row to whatever it's about ("proposal" / "12",
    "order" / "<broker_order_id>", ...). `payload_json` carries the
    structured data.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )


class Lot(Base):
    """Per-tax-lot cost-basis record (SDD §9.1).

    Imported from broker exports (Schwab CSV in v1; IBKR API later;
    Leumi: never — Leumi gives no per-lot data).
    """

    __tablename__ = "lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    lot_id_external: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    quantity: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    cost_basis_usd: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    acquired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Fill(Base):
    """One execution event (a partial or full fill).

    `paper=True` rows are PaperFill log entries; live executions have
    `paper=False`. A single proposal can have multiple fills (partials).
    """

    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    proposal_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("proposals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    broker: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    broker_order_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    price: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    commission: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    filled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
    paper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)


class PendingOrder(Base):
    """A broker order placed but not yet fully reconciled.

    The reconcile loop (SDD §10.5) polls these to drive proposals to
    EXECUTED_LIVE once filled, or back to BLOCKED/REJECTED on broker
    failure.
    """

    __tablename__ = "pending_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    proposal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    broker: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    broker_order_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="submitted", index=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


# ----------------------------------------------------------------------
# Phase 5: Argonaut limited account + daily P&L + TOTP secrets
# ----------------------------------------------------------------------


class ArgonautSnapshot(Base):
    """One per-day snapshot of the Argonaut limited-account state.

    Drives the P&L curve since inception on screen #5. Persisted by the
    daily-brief loop. `date` is YYYY-MM-DD for friendly indexing.
    """

    __tablename__ = "argonaut_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    total_value_usd: Mapped[float] = mapped_column(
        Numeric(18, 4), nullable=False, default=0
    )
    cash_usd: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    positions_value_usd: Mapped[float] = mapped_column(
        Numeric(18, 4), nullable=False, default=0
    )
    day_pnl_usd: Mapped[float] = mapped_column(
        Numeric(18, 4), nullable=False, default=0
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "account_id", "date", name="uq_argonaut_snapshots_user_acct_date"
        ),
    )


class DailyAccountPnL(Base):
    """Per-account, per-day realized + unrealized P&L roll-up.

    Drives the daily-loss-limit hard gate in the risk preflight. The
    reconcile loop writes/updates this row from fills as they arrive;
    `locked=True` marks the row as halted (no further trades for the day).
    """

    __tablename__ = "daily_account_pnl"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    realized_pnl_usd: Mapped[float] = mapped_column(
        Numeric(18, 4), nullable=False, default=0
    )
    unrealized_pnl_usd: Mapped[float] = mapped_column(
        Numeric(18, 4), nullable=False, default=0
    )
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "account_id", "date", name="uq_daily_account_pnl_user_acct_date"
        ),
    )


class TOTPSecret(Base):
    """Per-user TOTP secret for the T3 second-factor flow.

    `secret_encrypted` is the user's TOTP base32 secret. v1 stores it
    plain-text inside the DB; productization will move it to the OS
    keychain via `argosy.secrets`. `last_verified_at` advances on every
    successful verify so the API can detect replay (require monotonically
    increasing timestamps within a step window).
    """

    __tablename__ = "totp_secrets"

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ----------------------------------------------------------------------
# Phase 6: control-plane tenant registry + setup tokens
# ----------------------------------------------------------------------


class Tenant(Base):
    """Control-plane registry of provisioned tenants (SDD §12.5).

    One row per tenant. Lives in the *control* DB (the one ARGOSY_HOME
    points at). Per-tenant data DBs are derived from `db_path`.
    """

    __tablename__ = "tenants"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    db_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_active_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class SetupToken(Base):
    """One-time first-login token issued by `argosy admin tenant create`."""

    __tablename__ = "setup_tokens"

    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    # Phase 4
    "AuditLog",
    "Fill",
    "Lot",
    "PendingOrder",
    # Phase 5
    "ArgonautSnapshot",
    "DailyAccountPnL",
    "TOTPSecret",
    # Phase 6
    "Tenant",
    "SetupToken",
]
