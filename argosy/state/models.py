"""SQLAlchemy models for Argosy.

Phase 0: `users` and `user_context`.
Phase 1: `plan_versions`, `plan_critiques`, `agent_reports`,
`agent_reports_blobs`. Adds `current_stage` to `user_context`.
Phase 2: `cadence_state`, `daily_briefs`, `kv_cache`, `news_cache`,
`macro_cache`. (`kv_cache` was historically `prices_cache` — renamed in
migration 0011 because it's a generic key/value/TTL store, not a
prices-only table.)
Phase 3: `proposals`, `proposals_history`, `approvals`, `decision_runs`.
Phase 4: `audit_log`, `lots`, `fills`, `pending_orders`. The audit_log
table is the universal event log per SDD §14.1; lots holds per-lot
cost-basis from broker imports; fills records each execution event;
pending_orders tracks open broker orders awaiting reconciliation.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
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
    # Hardening: groups all turns of one intake conversation. Generated on
    # stage_1 entry; cleared (left as the last value) when stage_6 completes;
    # rotated on the next stage_1 entry. Stamped onto every agent_reports
    # row produced during this intake session.
    intake_session_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="context")


class PlanVersion(Base):
    """An imported or synthesized plan, with explicit lifecycle role.

    Roles per SDD §6.10:
      - baseline: user-imported source (Jacobs Wealth Plan v2.0). Carries
        distillate_json + distillate_rendered + source_hash. One active
        per user (partial unique index).
      - draft: synthesis output awaiting user accept. Carries horizon_*_*
        columns (added in 0017). One in-flight per user.
      - current: accepted draft, the canonical plan the advisor anchors on.
      - superseded: historical; demoted from baseline/current/draft.

    Lineage: derived_from_id points back to the source row a synthesized
    plan was built from (typically the active baseline at synthesis time).
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

    # Lifecycle (migration 0015).
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="baseline", server_default="baseline"
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    derived_from_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True
    )
    decision_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("decision_runs.id", ondelete="SET NULL"), nullable=True
    )

    # Distillate (migration 0016) — populated only when role='baseline'.
    distillate_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    distillate_rendered: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    distilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Synthesis (migration 0017) — populated only when role in {draft,current,superseded}.
    horizon_long_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    horizon_medium_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    horizon_short_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    horizon_long_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    horizon_medium_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    horizon_short_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    synthesis_inputs_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provenance Wave A (migration 0019) — points back at the catalog row
    # for the bytes this plan was imported from. Optional because synthesized
    # drafts and superseded historical rows have no source file.
    source_file_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user_files.id", ondelete="SET NULL"), nullable=True
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


class UserFile(Base):
    """One row per stored byte-blob per user (Wave A — provenance catalog).

    Every user-supplied file flows through the single boundary helper
    ``argosy/services/file_catalog.py::catalog_upload`` and lands here.
    Re-uploads of the same content (same sha256, same user) collapse into
    the existing row instead of creating duplicates — enforced by the
    partial unique index ``ix_user_files_user_sha256_active`` (see
    migration 0019). Soft-delete via ``deleted_at`` lets a user remove a
    file and re-upload identical bytes later without colliding with the
    tombstone.

    ``storage_path`` is the absolute path on disk. The new layout is
    ``<ARGOSY_HOME>/uploads/<user_id>/<YYYY>/<YYYY-MM-DD>/<HHMMSS>__<sha8>__<sanitized>``.
    Existing Wave 5 paths under
    ``<ARGOSY_HOME>/uploads/<user_id>/<turn_uuid>/<file>`` continue to
    work — the backfill CLI inserts rows pointing at them.
    """

    __tablename__ = "user_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    sanitized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    # Allowed kinds: text / image / plan_markdown / broker_csv / other.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Allowed sources: chat_attachment / intake_upload / intake_file_to_text
    # / cost_basis_import. Tracks ingest channel for filtering / audit.
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # Optional context fields — set by the helper depending on the source.
    turn_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    intake_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True
    )
    decision_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("decision_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
    # Hardening: groups all agent calls produced during one intake session.
    # Mutually exclusive with decision_id in practice — intake agents stamp
    # this; decision-flow agents stamp decision_id.
    intake_session_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
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
    # Provenance Wave C — back-link to the negotiation phase this run
    # participated in (NULL for one-shot agents that aren't part of a
    # multi-agent debate). Migration 0020.
    phase_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("decision_phases.id", ondelete="SET NULL"), nullable=True
    )

    blobs: Mapped[list["AgentReportBlob"]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )


class DecisionPhase(Base):
    """One row per phase boundary in a multi-agent flow (Wave C — provenance).

    A *phase* aggregates the agent runs that produced one structured
    verdict — e.g. the bull/bear debate + facilitator → ``DebateOutcome``,
    the three risk officers + facilitator → ``RiskOutcome``, the trader's
    proposal → ``TraderProposal``, the fund manager's call →
    ``FundManagerDecision``. Plan-synthesis 5-phase and amendment
    Medium/Large workers also write rows.

    ``verdict_json`` is the ``model_dump_json()`` of the corresponding
    pydantic DTO (no DTOs redefined for the catalog — they live next to
    their agents in ``argosy/agents/``). ``verdict_kind`` is the DTO class
    name so the UI picks a renderer without sniffing fields.

    ``bundle_dir`` is the absolute filesystem path under
    ``<ARGOSY_HOME>/transcripts/<user_id>/<YYYY-MM-DD>/<run_id>__<kind>/``
    containing the full ``TLDR.md`` / ``transcript.md`` / ``verdict.json``
    / ``sequence.mmd`` mirror.
    """

    __tablename__ = "decision_phases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("decision_runs.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # JSON list of {agent_role, agent_report_id, side?, perspective?, round?}.
    participants_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # JSON of model_dump_json() of the parsed pydantic DTO; NULL when the
    # phase has no facilitator (e.g. analyst phase with no aggregator).
    verdict_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tldr_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    bundle_dir: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    # `(decision_run_id, seq)` must be unique. The recorder computes
    # `next_seq = max(seq) + 1` without locking, so the serial-caller
    # contract documented in SDD §17.2 is enforced at the DB level
    # via migration 0025's unique INDEX (not constraint — they're
    # equivalent on SQLite but distinct objects on PostgreSQL; this
    # model declaration matches the migration's `create_index(unique=True)`
    # so the two never drift). Also serves as the lookup index that
    # drives the Replay endpoint.
    __table_args__ = (
        Index(
            "ix_decision_phases_run_seq",
            "decision_run_id", "seq",
            unique=True,
        ),
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


class KvCacheEntry(Base, _CacheBase):
    """Generic per-(provider, key) JSON cache with TTL.

    Despite the legacy ``prices_cache`` name, this table stores cached
    payloads for any kind that fits the ``CacheKind`` enum — prices,
    news (when callers route here vs. ``news_cache``), and UI snapshots
    (``CacheKind.UI``) such as the home-brief composition.
    """

    __tablename__ = "kv_cache"


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
    # tier: originally a 4-char trade-tier sentinel ("T0"/"T3"); migration
    # 0018 widened it to String(8) and made it nullable so the same column
    # can also carry amendment-tier values ("small"/"medium"/"large") for
    # plan-amendment-chat runs (Wave 4). decision_kind disambiguates which
    # value shape is in play.
    tier: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # decision_kind: "trade_proposal" (default, per-trade runs), "plan_revision"
    # (synthesis runs), or "plan_amendment_chat" (Wave 4 chat-driven amendment
    # runs). Added by migration 0015_plan_versions_lifecycle.
    decision_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="trade_proposal", server_default="trade_proposal"
    )
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
    # notes_json: free-form JSON-serialized notes for plan-amendment-chat
    # runs (the user's amendment message + the parsed AmendmentIntent for
    # replay). Added by migration 0018. NULL for trade_proposal /
    # plan_revision runs.
    notes_json: Mapped[str | None] = mapped_column(Text, nullable=True)


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


# ----------------------------------------------------------------------
# Phase 3 (Israeli pension data): pension_fund_snapshots
# ----------------------------------------------------------------------


class PensionFundSnapshot(Base):
    """One per-user, per-fund snapshot of gemelnet performance data.

    The `argosy gemelnet refresh-user` command writes one row per
    fund in the user's `identity.pensions` list each time it runs.
    Time-series: query by `(user_id, fund_id)` ordered by
    `snapshot_at DESC` to get the most recent snapshot. The
    `source_url` column is auditability sugar — every claim made by
    a downstream agent can quote the row id and its `source_url` so a
    reader can hop to the underlying public MoF page.
    """

    __tablename__ = "pension_fund_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    fund_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fund_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    fund_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    manager: Mapped[str | None] = mapped_column(String(128), nullable=True)
    return_pct_12m: Mapped[float | None] = mapped_column(
        Numeric(8, 4), nullable=True
    )
    benchmark_return_pct_12m: Mapped[float | None] = mapped_column(
        Numeric(8, 4), nullable=True
    )
    relative_to_benchmark_pct: Mapped[float | None] = mapped_column(
        Numeric(8, 4), nullable=True
    )
    balance_nis: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        Index(
            "ix_pension_fund_snapshots_user_fund_time",
            "user_id",
            "fund_id",
            "snapshot_at",
        ),
    )


class InvestorEvent(Base):
    """One investor-event row from a Phase 4 adapter.

    Sources: ``sec_form4`` (insider trades), ``sec_13f`` (institutional
    quarterly filings), ``tipranks`` (analyst-consensus snapshots),
    ``capitoltrades`` (STOCK Act disclosures). Adapters write through the
    ``record_investor_events(...)`` helper after a successful pull.

    The home-brief signal bullet picks the most-recent row by
    ``occurred_at DESC`` and surfaces a one-liner. Querying by ``user_id``
    keeps the row scoped to the user the daily-brief loop ran for.

    Dedup: every row carries a ``unique_key`` derived from natural keys
    in the source payload (e.g. ``ticker:accession`` for Form 4,
    ``ticker:url`` for news). The unique constraint on
    ``(user_id, source, unique_key)`` lets the writer use
    ``INSERT ... ON CONFLICT DO NOTHING`` so the same insider trade
    landing in 30 consecutive daily-brief ticks produces one row, not
    30. ``unique_key`` is required and persisted as the canonical
    de-duplication anchor.

    Indexed on ``(user_id, occurred_at)`` so the home-brief query is an
    index seek; ``(user_id, source, ticker)`` lets us extend later for
    per-source / per-ticker drilldowns without a table scan.
    """

    __tablename__ = "investor_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    ticker: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    headline: Mapped[str] = mapped_column(Text, nullable=False, default="")
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Natural-key digest derived from the source payload. Used (with
    # ``user_id`` and ``source``) to gate idempotent inserts so the
    # same Form 4 / 13F / news / consensus row landing on N consecutive
    # daily-brief ticks produces one row, not N.
    unique_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    __table_args__ = (
        Index(
            "ix_investor_events_user_occurred",
            "user_id",
            "occurred_at",
        ),
        Index(
            "ix_investor_events_user_source_ticker",
            "user_id",
            "source",
            "ticker",
        ),
        UniqueConstraint(
            "user_id",
            "source",
            "unique_key",
            name="uq_investor_events_user_source_uniquekey",
        ),
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
    "KvCacheEntry",
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
    # Phase 3 (Israeli pension)
    "PensionFundSnapshot",
    # Phase 4 (investor events)
    "InvestorEvent",
    # Household expenses subsystem
    "ExpenseSource",
    "ExpenseStatement",
    "ExpenseCategory",
    "ExpenseTransaction",
    "MerchantCategoryCache",
    "ExpenseReviewQueue",
    # FX rate cache (Wave EX1.1 — migration 0023)
    "FxRate",
]


# ----------------------------------------------------------------------
# Household expenses subsystem (Wave EX1 — migration 0021)
# ----------------------------------------------------------------------


class ExpenseSource(Base):
    """A bank account or credit card the user has registered for expense ingest.

    Cardholder is metadata only — household aggregation is the unit; spend rolls
    to a single pool regardless of `cardholder_name`. ``kind`` distinguishes
    bank current accounts from credit cards; ``external_id`` is the card last-4
    or bank account number, stable across months.
    """

    __tablename__ = "expense_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    issuer: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    cardholder_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "kind", "external_id",
                         name="uq_expense_sources_user_kind_external"),
    )


class ExpenseStatement(Base):
    """A single uploaded statement file's metadata. Idempotent on
    (user_id, source_id, period_start, period_end).
    """

    __tablename__ = "expense_statements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("expense_sources.id", ondelete="CASCADE"),
        nullable=False
    )
    file_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user_files.id", ondelete="RESTRICT"),
        nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    charge_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    declared_total_nis: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    parsed_total_nis: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    parser_name: Mapped[str] = mapped_column(String(32), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "source_id", "period_start", "period_end",
                         name="uq_expense_statements_user_source_period"),
    )


class ExpenseCategory(Base):
    """Hierarchical taxonomy. user_id NULL = system-default row (copied per user
    on first ingest). is_excluded_from_spend marks rows that render but don't
    aggregate as 'real spending' (transfers, investments, taxes paid).
    """

    __tablename__ = "expense_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    label_en: Mapped[str] = mapped_column(String(64), nullable=False)
    label_he: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("expense_categories.id", ondelete="SET NULL"),
        nullable=True
    )
    is_excluded_from_spend: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_inflow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="uq_expense_categories_user_slug"),
    )


class ExpenseTransaction(Base):
    """One transaction row, persisted from a parsed statement.

    Aggregation rules:
      real_spending(month) = SUM(amount_nis) WHERE direction='debit'
                             AND category.is_excluded_from_spend = FALSE
                             AND category.is_inflow = FALSE
                             AND is_card_payment = FALSE
      real_income(month)   = SUM(amount_nis) WHERE direction='credit'
                             AND category.is_inflow = TRUE
    Refunds offset within their inherited category via refund_of_id.
    """

    __tablename__ = "expense_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    statement_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("expense_statements.id", ondelete="CASCADE"),
        nullable=False
    )
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("expense_sources.id", ondelete="CASCADE"),
        nullable=False
    )
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    posted_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    merchant_raw: Mapped[str] = mapped_column(String(512), nullable=False)
    merchant_normalized: Mapped[str] = mapped_column(String(512), nullable=False)
    amount_nis: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    amount_orig: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency_orig: Mapped[str | None] = mapped_column(String(3), nullable=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    tx_type: Mapped[str] = mapped_column(String(16), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("expense_categories.id", ondelete="SET NULL"),
        nullable=True
    )
    category_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    category_confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    is_card_payment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    matched_statement_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("expense_statements.id", ondelete="SET NULL"),
        nullable=True
    )
    refund_of_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("expense_transactions.id", ondelete="SET NULL"),
        nullable=True
    )
    raw_row_json: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON-encoded list of tags (e.g. '["trip:greece-2026-aug"]'). Tags
    # overlay on top of category — a row can be food.restaurants AND
    # trip:greece-2026-aug simultaneously. Migration 0024.
    tags: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]",
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class MerchantCategoryCache(Base):
    """Per-user cache mapping a normalized merchant pattern to a category.
    User overrides (source='user') always win; LLM results (source='llm')
    only persist when confidence ≥ 0.85.
    """

    __tablename__ = "merchant_category_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    merchant_pattern: Mapped[str] = mapped_column(String(512), nullable=False)
    is_regex: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("expense_categories.id", ondelete="CASCADE"),
        nullable=False
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "merchant_pattern", "is_regex",
                         name="uq_merchant_category_cache"),
    )


class ExpenseReviewQueue(Base):
    """Anomalies + uncategorized rows pending user review.
    Built by the anomaly detector (EX2) and the orchestrator (EX1, for
    uncategorized rows).
    """

    __tablename__ = "expense_review_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    related_tx_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("expense_transactions.id", ondelete="SET NULL"),
        nullable=True
    )
    related_source_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("expense_sources.id", ondelete="SET NULL"),
        nullable=True
    )
    user_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ----------------------------------------------------------------------
# FX rate cache (Wave EX1.1 — migration 0023)
# ----------------------------------------------------------------------


class FxRate(Base):
    """Daily exchange-rate cache. Rates stored as units of ILS per 1 unit of currency.

    Source today: Bank of Israel representative rates (boi.org.il).
    """

    __tablename__ = "fx_rates"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    currency: Mapped[str] = mapped_column(String(8), primary_key=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="boi"
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
