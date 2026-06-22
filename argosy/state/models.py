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
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text as _sa_text,
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

    # Spec D commit #1 (migration 0054): banner-acknowledgement
    # timestamp for the /life-events conversion-assistant.  NULL = user
    # has unacknowledged conversion-log entries (banner visible);
    # non-NULL = "I've reviewed all conversions" clicked.  Migration
    # 0054 auto-sets this to the migration timestamp for users with
    # no legacy life_events rows so the banner never appears for them.
    life_events_migration_acknowledged_at: Mapped[datetime | None] = (
        mapped_column(DateTime(timezone=True), nullable=True)
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
    # Migration 0061 — full-fidelity audit variants alongside the
    # user-facing horizon_*_md (which drops status header, revisit
    # parentheticals, and the "Deltas vs. prior current" block).
    horizon_long_md_audit: Mapped[str | None] = mapped_column(Text, nullable=True)
    horizon_medium_md_audit: Mapped[str | None] = mapped_column(Text, nullable=True)
    horizon_short_md_audit: Mapped[str | None] = mapped_column(Text, nullable=True)
    synthesis_inputs_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Persisted bilingual plan narrative (migration 0062). JSON blob of
    # {narrative_md_en, narrative_md_he, confidence} produced by the
    # PlanNarrativeAgent. Written through on first generation so the
    # /plan recap survives a backend restart and loads instantly instead
    # of re-running the LLM (the prior cache was process-local only).
    # NULL until the narrative is first generated for this plan version.
    narrative_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Canonical instrument-level target allocation (roadmap T1.x) — the
    # TargetAllocationDoc the deterministic allocation_plan engine authors and
    # every surface (/plan glidepath, /portfolio target, /retirement glide)
    # projects. NULL on plan versions written before the doc existed; populated
    # forward on synthesis and backfilled for the current plan.
    target_allocation_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Structured synthesis sections (the flat ``PlanSynthesisOutput.sections``
    # list — each Section carries its own ``horizon`` + evidence contract).
    # The synthesizer already produces these at runtime; persisting them lets
    # the plan-output gate evaluate section_coverage + evidence_per_section
    # against the REAL sections at promote-time instead of reconstructing a
    # sectionless object from the per-horizon JSON. NULL on plan versions
    # written before this column existed (legacy → gate WARNs, never blocks).
    sections_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provenance Wave A (migration 0019) — points back at the catalog row
    # for the bytes this plan was imported from. Optional because synthesized
    # drafts and superseded historical rows have no source file.
    source_file_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user_files.id", ondelete="SET NULL"), nullable=True
    )

    critiques: Mapped[list["PlanCritique"]] = relationship(
        back_populates="plan_version", cascade="all, delete-orphan"
    )


class CoherenceDecision(Base):
    """A durable, machine-checkable coherence ruling. Versioned/supersedable:
    a replacement supersedes the prior row (which is retained for audit)."""

    __tablename__ = "coherence_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    dispute_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ruling: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    basis: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    resolved_by: Mapped[str] = mapped_column(String(16), nullable=False)
    coherence_invariant_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    conformed_surfaces_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    superseded_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("coherence_decisions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class TaxSimulationLot(Base):
    """One sellable RSU/ESPP lot from a Schwab/ESOP simulated tax report.

    The load-bearing field is ``eligible`` (Holding Period == "OK" → Section-102 capital
    track ~25%, vs "Breaking" → ordinary income ~62%), which makes the NVDA
    deconcentration a lot-exact, tax-aware schedule. Re-ingesting a report supersedes the
    prior lots for the same (user_id, simulation_date)."""

    __tablename__ = "tax_simulation_lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    simulation_date: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    plan_type: Mapped[str] = mapped_column(String(8), nullable=False)  # RSU | ESPP
    shares: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    holding_period: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    grant_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    grant_date: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    purchase_date: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    sale_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_basis_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    capital_income_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    ordinary_income_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_proceeds_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_file_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
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
    # Wave A — Anthropic Messages API telemetry (migration 0026).
    cache_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    cache_creation_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    thinking_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    citations_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None,
    )
    # Wave B-UI Task 9 — serialised list of KB/document sources injected into
    # the prompt (migration 0027). NULL when agent returned a 2-tuple prompt.
    sources_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None,
    )
    # Wave B-UI follow-up Item 2 — uuid4 correlation id threaded through
    # BaseAgent.run() and the WS events (migration 0028). NULL for rows
    # persisted before this migration; the UI hook falls back to the ±10s
    # heuristic for those.
    run_correlation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, default=None,
    )
    # Wave B-UI follow-up Item B — full system + user prompts captured in
    # BaseAgent.run() (migration 0029). NULL for rows persisted before this
    # migration; the /prompt endpoint surfaces this as a "Prompt not captured"
    # empty state. Stored separately from response_text because they can be
    # 10-100KB each and are only needed when the Prompt tab opens.
    system_prompt: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None,
    )
    user_prompt: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None,
    )
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
    # T2.3 — full phase output captured at phase completion. Lets a retry
    # load completed phases from DB and skip re-running them instead of
    # forfeiting their cost. Stores text for analyst/debate phases,
    # model_dump_json for synthesizer output.
    phase_output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class PortfolioSnapshotRow(Base):
    """Persisted snapshot of the parsed Family Finances Status TSV.

    Replaces the filesystem-walk + parse pattern in
    ``argosy.orchestrator.flows.plan_synthesis.inputs._find_latest_tsv`` —
    once populated, downstream callers (``/api/portfolio/snapshot``, the
    synthesis input assembler, the NVDA trajectory endpoint) read from
    this table by ``(user_id, ORDER BY imported_at DESC LIMIT 1)``.

    The writer fires from the TSV ingest path and from
    ``/api/portfolio/snapshot`` as a write-through cache when no fresh
    DB row exists. Migration 0030 creates the schema.

    JSON-serialised columns mirror the PortfolioSnapshot pydantic model:

    * ``positions_json``         — list[PortfolioPosition]
    * ``allocations_json``       — list[AllocationRow]
    * ``nvda_sales_json``        — list[NVDASale]
    * ``real_estate_json``       — list[RealEstatePosition]
    * ``pensions_json``          — list[PensionEntry]
    * ``totals_json``            — {total_usd_value_k, cash_balances_usd_k}
    * ``parse_warnings_json``    — list[str]
    """

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    snapshot_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    positions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allocations_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    nvda_sales_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    real_estate_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    pensions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    totals_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    fx_usd_nis: Mapped[float | None] = mapped_column(Float, nullable=True)
    fx_usd_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    parse_warnings_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )


class RealEstatePayment(Base):
    """Canonical ledger of payments made toward a property purchase.

    The portfolio snapshot (from the Family-Finances TSV) carries a per-property
    Home (contract price) and Loan (remaining-to-pay) row, but the Loan is a
    static figure that drifts and is overwritten on every re-import. This table
    is the durable source of truth for what's been PAID: each row is one payment
    (an invoice / advance / installment), and the remaining balance is COMPUTED
    as ``contract price − Σ(net payments)`` so it survives TSV re-imports and is
    auditable to the source documents.

    ``amount_net_local`` is the equity-building (ex-VAT) amount in the property
    currency; ``vat_local`` is tax paid alongside it (a sunk cost, not equity).
    ``property_key`` matches the snapshot ``location`` (e.g. "Pipera"). An
    aggregate ``kind='opening'`` row captures payments made before the ledger
    existed so the computed paid-to-date is complete. ``source_file_id`` links to
    the uploaded invoice in ``user_files`` for the audit trail.
    """

    __tablename__ = "real_estate_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    property_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    invoice_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_net_local: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    vat_local: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="EUR")
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="installment")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_file_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


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


class JobRun(Base):
    """One execution row for the JobRegistry audit log.

    Spec A (jobs-registry) §2.1. Written by the registry's
    ``_open_job_run`` / ``_close_job_run`` helpers (commit #3a). Every
    scheduled tick + every manual ``POST /api/jobs/{name}/run-now``
    invocation produces exactly one row.

    Distinct from ``CadenceState``: the latter is a per-loop POINTER
    (last tick, next due) that the scheduler reads each iteration. This
    is a per-EXECUTION audit trail the registry + UI use for history.
    Both are written under §1.7's dual-write contract.
    """

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_trigger: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=_sa_text("0")
    )
    triggered_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )


class DailyBrief(Base):
    """One Daily Brief run record.

    Two-shape table — Phase 2 columns + T4.5 (migration 0034) columns:

    - Phase 2 (legacy ``DailyBriefLoop``): ``run_at`` + four analyst
      report-JSON columns + ``summary_text`` composed from them. The
      ``DailyBriefLoop.tick`` in ``argosy/orchestrator/loops/daily_brief.py``
      writes these.
    - T4.5 (``daily_brief_runner.generate_daily_brief``): ``brief_date``
      (calendar key for idempotency), ``content_md`` (the one-pager
      markdown rendered by ``DailyBrieferAgent``), and
      ``decision_run_id`` pointing back at the ``decision_runs`` row
      that produced it.

    Both shapes coexist so the legacy loop + the legacy
    ``/api/daily-brief/latest`` route keep working unchanged. The
    T4.5 runner leaves the four analyst-report-JSON columns empty
    and lets ``content_md`` carry the user-facing brief.
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
    # T4.5 — one-pager markdown produced by ``DailyBrieferAgent``.
    # Empty string for legacy rows from the Phase 2 loop.
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # T4.5 — calendar date the brief covers; NULL for legacy rows.
    # Partial UNIQUE index ``uq_daily_briefs_user_date`` enforces one
    # row per (user_id, brief_date) for new rows so the runner is
    # idempotent on the same calendar day.
    brief_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # T4.5 — back-pointer to ``decision_runs`` (decision_kind='daily_brief').
    # NULL for legacy rows that pre-date the runner.
    decision_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("decision_runs.id"), nullable=True
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
    # T4.4: audit lineage — the canonical plan version this proposal traces to.
    # A plain nullable reference to plan_versions.id (not a DB-enforced FK, to
    # keep the SQLite ADD COLUMN migration simple); stamped best-effort at
    # persist time, NULL when no current plan exists.
    plan_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    "CoherenceDecision",
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
    # Fleet self-review (migration 0037)
    "FleetSelfReviewReport",
    # Anomaly detection (EX2 — migration 0038)
    "AnomalyReport",
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

    Migration 0047 added three columns (sprint #2 anomaly detection):
      * ``materiality`` — info/warning/critical severity ladder.
      * ``dedup_key``   — deterministic stable key per anomaly type;
        version-prefixed ``v1|...`` so future rule changes get fresh
        keys without false suppression. Formulas per pattern documented
        in spec #2 §4.
      * ``bucket``      — categorical: amount/recurring/cache/duplicate.

    Partial unique index on ``(user_id, dedup_key)`` where
    ``dedup_key IS NOT NULL AND status = 'open'`` keeps the detector
    idempotent across reruns.
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
    # Sprint #2 anomaly extensions (migration 0047).
    materiality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    bucket: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "materiality IS NULL OR materiality IN ("
            "'info', 'warning', 'critical')",
            name="ck_expense_review_queue_materiality",
        ),
        CheckConstraint(
            "bucket IS NULL OR bucket IN ("
            "'amount', 'recurring', 'cache', 'duplicate')",
            name="ck_expense_review_queue_bucket",
        ),
        Index(
            "ix_expense_review_queue_dedup",
            "user_id",
            "dedup_key",
            unique=True,
            sqlite_where=_sa_text(
                "dedup_key IS NOT NULL AND status = 'open'"
            ),
            postgresql_where=_sa_text(
                "dedup_key IS NOT NULL AND status = 'open'"
            ),
        ),
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


# ----------------------------------------------------------------------
# FM objection plain-English translation cache (migration 0035)
# ----------------------------------------------------------------------


class FMObjectionTranslation(Base):
    """Precomputed plain-English translation of one FM objection.

    Owned by a draft (``plan_version_id`` FK with CASCADE delete) and
    keyed within the draft by ``objection_index`` (the position in the
    list emitted by ``GET /api/plan/draft/objections``).

    Filled eagerly on the first call to that endpoint by
    ``argosy.services.fm_objection_translation_cache.get_or_compute_translations``
    so that subsequent loads of the same draft return translations
    inline without paying the Sonnet round-trip again — the UI toggle
    between "original Fund Manager wording" and "plain English" is then
    instant.

    ``topic_hash`` is sha256 of ``(severity, topic, detail)``; the cache
    helper re-translates a slot whose stored hash no longer matches the
    live FM text (defense in depth — the FM objection list is meant to
    be stable per ``decision_run_id``, but this guards against any
    upstream re-evaluation slipping the text under us).
    """

    __tablename__ = "fm_objection_translations"
    __table_args__ = (
        UniqueConstraint(
            "plan_version_id",
            "objection_index",
            name="uq_fm_objection_translations_plan_idx",
        ),
        Index(
            "ix_fm_objection_translations_plan_version",
            "plan_version_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    plan_version_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("plan_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    objection_index: Mapped[int] = mapped_column(Integer, nullable=False)
    topic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False, default="")
    plain_english: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recommended_actions_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


# ----------------------------------------------------------------------
# Per-FM-objection user stance (migration 0036)
# ----------------------------------------------------------------------


class FMObjectionUserState(Base):
    """Per-(user, plan_version, objection_index) stance on a FM objection.

    Added by migration 0036. Lets the user mark each Fund Manager
    objection AGREE / DISAGREE / DEFER and, when disagreeing, attach a
    free-text counter-position. The companion
    ``POST /api/plan/draft/objections/start-new-round`` endpoint reads
    every row for the draft, composes a structured guidance string from
    the stances + counter-positions, and routes through the existing
    advisor check-in flow so the cost-cap wiring is reused.

    ``topic_hash`` is defense-in-depth — the FM objection list is parsed
    live from ``fund_manager`` agent_report.response_text on every GET,
    so if the list shifts between renders we can detect a stale row.

    DEFER is the default state on the UI side; rows for DEFER objections
    are still written so we can render the same state after navigating
    away and back.
    """

    __tablename__ = "fm_objection_user_state"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "plan_version_id",
            "objection_index",
            name="uq_fm_obj_state_per_objection",
        ),
        Index(
            "ix_fm_obj_state_plan",
            "plan_version_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_version_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("plan_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    objection_index: Mapped[int] = mapped_column(Integer, nullable=False)
    topic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    stance: Mapped[str] = mapped_column(String(16), nullable=False)
    counter_position: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )
    # Wave 7 Piece B — stance carry-forward audit fields. NULL on fresh
    # (user-typed) rows; populated by the carry-forward matcher when a
    # prior-draft objection was carried over into this row.
    matched_from_plan_version_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    match_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    match_top2_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_model_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )


# ----------------------------------------------------------------------
# Fleet self-review report (migration 0037)
# ----------------------------------------------------------------------


class FleetSelfReviewReport(Base):
    """One persisted output of the ``FleetSelfReviewAgent`` runner.

    The runner fires automatically:

      * ``scope_kind='post_synthesis'`` — after each plan_revision
        ``decision_runs`` completion (orchestrator hook fires the runner
        on a background thread once the draft + verdict are persisted).
        ``decision_run_id`` points at the synthesis run that just
        finished.
      * ``scope_kind='daily'``         — once a day alongside the daily
        brief (gated by ``ARGOSY_DAILY_BRIEF_ENABLED=1``).
        ``decision_run_id`` is NULL because the sweep is portfolio-wide.
      * ``scope_kind='manual'``        — ``argosy fleet self-review``
        CLI / a future on-demand admin action.

    ``content_md`` is the human-readable markdown report.  The optional
    LLM-composed top section is appended after the deterministic
    detector output — detector findings are NEVER hallucinated.

    ``findings_json`` is a list of ``Finding`` dataclasses
    (``id``, ``detector``, ``severity``, ``category``, ``title``,
    ``evidence``, ``suggested_fix``).  ``severity_summary_json`` is the
    pre-joined ``{"RED": N, "AMBER": M, "YELLOW": K}`` for the
    home-page badge.
    """

    __tablename__ = "fleet_self_review_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # 'post_synthesis' | 'daily' | 'manual' — DB CHECK constraint enforces.
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("decision_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    findings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    severity_summary_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default='{"RED":0,"AMBER":0,"YELLOW":0}',
    )

    __table_args__ = (
        Index(
            "ix_fleet_self_review_user_generated",
            "user_id",
            "generated_at",
        ),
        Index(
            "ix_fleet_self_review_decision_run",
            "decision_run_id",
        ),
    )


# ----------------------------------------------------------------------
# Anomaly-detection report (EX2 — migration 0038)
# ----------------------------------------------------------------------


class AnomalyReport(Base):
    """One persisted output of the ``AnomalyDetectionAgent`` runner (EX2).

    The runner fires automatically:

      * ``triggered_by='event'``  — after every Discount Bank statement
        ingest (event-driven path so a same-day fee-waiver disappearance
        surfaces within seconds, not 24h).  ``source_statement_id``
        points at the statement that just landed.
      * ``triggered_by='daily'``  — once a day alongside the daily
        brief (gated by ``ARGOSY_ANOMALY_DETECTION_ENABLED=1``).
        ``source_statement_id`` is NULL.
      * ``triggered_by='manual'`` — explicit ``POST /api/anomalies/run``
        from the UI / CLI.

    ``report_json`` is the ``AnomalyDetectionReport`` pydantic model
    serialized.  ``severity_summary_json`` is the pre-joined
    ``{"RED": N, "AMBER": M, "YELLOW": K}`` so the home-page banner can
    render the severity counters without parsing the full report.
    """

    __tablename__ = "anomaly_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 'event' | 'daily' | 'manual' — DB CHECK constraint enforces.
    triggered_by: Mapped[str] = mapped_column(String(16), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    source_statement_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("expense_statements.id", ondelete="SET NULL"),
        nullable=True,
    )
    report_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    severity_summary_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default='{"RED":0,"AMBER":0,"YELLOW":0}',
    )
    agent_report_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("agent_reports.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "ix_anomaly_reports_user_triggered",
            "user_id",
            "triggered_at",
        ),
        Index(
            "ix_anomaly_reports_source_statement",
            "source_statement_id",
        ),
    )


class AllocationAction(Base):
    """One row per user Accept/Defer on an allocation proposal.

    Holds decisions from any allocation flow: the original windfall
    detector (`action_source='windfall'`), the unallocated-cash tile
    (`'unallocated_cash'`), the monitor agent's drift trigger
    (`'monitor_drift'`), or other future allocation producers
    (`life_event` / `rebalance` / `manual`).

    Kept separate from the trade-order `proposals` table because the
    shape diverges (proposals carry ticker/action/order_type/tier/
    cooling_off; allocation actions carry horizon/asset_class/
    closes_delta_usd). Folding them together would force fake values on
    one side or lose fields on the other (codex zigzag review of spec
    `2026-05-29-plan-execute-monitor-reorg-design.md`, BLOCKER #2).

    Lifecycle: row created on Accept (decided_status='accepted') or
    Defer (decided_status='deferred' + optional due_date). When the
    action_engine later promotes the acceptance into a real trade
    proposal, the proposal_id FK is filled and decided_status moves
    to 'executed'. 'expired' is for actions stale enough that the
    market context has shifted (e.g. accepted 6mo ago, never acted on).

    Renamed from `WindfallAction` in migration 0041 — the class name is
    historically referenced as `WindfallAction` in legacy code paths,
    where a Python-level alias keeps imports working during transition.
    """

    __tablename__ = "allocation_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Discriminator: which detector / flow produced this action.
    # CHECK constraint enforced at the DB level (see migration 0041).
    action_source: Mapped[str] = mapped_column(String(32), nullable=False)
    # When the source event was detected (windfall detector run time,
    # unallocated-cash check, monitor drift sample, life-event create
    # time). Always populated; used by chronological ordering + dedup.
    source_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Source-specific reference. Nullable for 'manual' entries. Format
    # per source type (see spec §7):
    #   windfall / unallocated_cash → TSV path string
    #   monitor_drift → JSON {"snapshot_date": ..., "row": "Growth"}
    #   life_event → JSON {"life_event_id": 17}
    #   rebalance → JSON {"plan_draft_id": 12}
    #   manual → JSON {"user_note": "..."}
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Proposal shape, copied verbatim from AllocationProposal at the
    # moment of acceptance. Frozen at decision time so a later allocator
    # tweak doesn't retroactively change what the user signed off on.
    horizon: Mapped[str] = mapped_column(String(8), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_usd: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    closes_delta_usd: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)

    # User decision
    decided_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="accepted"
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    user_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # FK out to the eventual trade proposal when this gets promoted
    # through action_engine. Null until the promotion step runs.
    proposal_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("proposals.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "ix_allocation_actions_user_decided",
            "user_id",
            "decided_at",
        ),
        # Codex IMPORTANT #2: decided_at NOT in the unique key. Otherwise
        # two Accepts at different millisecond timestamps would not
        # collide -- the dedup intent (one decision per source row)
        # would be defeated. Route returns 409 on duplicate; if the user
        # wants to change a decision, the route does UPDATE not INSERT.
        Index(
            "ix_allocation_actions_source_unique",
            "user_id",
            "action_source",
            "source_ref",
            unique=True,
            sqlite_where=_sa_text("source_ref IS NOT NULL"),
            postgresql_where=_sa_text("source_ref IS NOT NULL"),
        ),
    )


# Legacy alias for import paths that still reference WindfallAction.
# Remove in a follow-on commit once all consumers move to the new name.
WindfallAction = AllocationAction


class LifeEvent(Base):
    """One row per user-recorded life event (career / family / asset /
    expense / recurring / retirement-milestone).

    Feeds:
      - cashflow_projection.effective_retire_ready_age() clamps for
        retirement_milestone:target_retire_year_change + blocking
        expense_event entries.
      - <HolisticTimelineCard> overlay markers on /retirement.
      - Monitor agent context (NOT trigger — per Ariel's Q2 answer life
        events feed interpretation, not direct red flags).

    Category enum enforced at DB layer; per-category kind enum enforced
    by Pydantic at the service layer (see argosy/services/life_events.py
    in sprint commit #8). The split is deliberate: kind values vary by
    category and encoding the relationship in SQL would add complexity
    without material safety over the Pydantic contract.

    Migration: alembic 0042.
    """

    __tablename__ = "life_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_usd: Mapped[float | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    recurring_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Forward-looking FK candidate; no constraint in v1.
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )

    # Spec D commit #1 (migration 0054): cashflow-shape extension.
    # ``delta_kind`` is the new discriminator over the five cashflow
    # shapes (one_shot / recurring_every_n_years / phase_change_start /
    # phase_change_end / none).  Existing rows default to ``none`` via
    # the DB server_default; the data conversion in migration 0054
    # promotes the documented cases (retirement_milestone with
    # target_date, expense_event:college, other_asset_acquired with
    # amount) into their target shapes.  Per-shape value columns are
    # nullable; the writer (Pydantic discriminator, commit #4) enforces
    # shape consistency.
    delta_kind: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="none", default="none"
    )
    monthly_delta_usd: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    one_shot_amount_usd: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    recurring_amount_usd: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    recurring_period_years: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    phase_start_date: Mapped[date | None] = mapped_column(
        Date, nullable=True
    )
    phase_end_date: Mapped[date | None] = mapped_column(
        Date, nullable=True
    )
    # FX (USD->NIS) locked at write time per spec §1.4 / IMPORTANT #4.
    fx_at_event: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("ix_life_events_user_date", "user_id", "target_date"),
        Index("ix_life_events_user_category", "user_id", "category"),
    )


class LifeEventMigrationLog(Base):
    """Audit row per ``life_events`` row touched by migration 0054.

    Spec D commit #1.  One row per source life_event that the
    upgrade saw — even rows that fell through to the
    ``delta_kind=none`` / ``conversion_outcome='lossy_converted'``
    bucket — so the user-facing banner on /life-events can correctly
    surface the conversion count and so the conversion-assistant UI
    (commit #5) has a per-row record to attach a ``user_decision`` to.

    Fields:
      * ``original_life_event_id`` — FK to ``life_events.id`` with
        ``ON DELETE CASCADE``; deleting the underlying event also
        tombstones its log entry.
      * ``original_kind`` / ``original_amount_usd`` — captured at
        upgrade time so a future downgrade / rollback can reconstruct
        the legacy shape even after commit #4 starts editing the
        per-shape columns.
      * ``target_delta_kind`` — one of the five delta_kind values.
      * ``conversion_outcome`` — one of ``preserved`` /
        ``lossy_converted`` / ``flagged_review``.
      * ``user_decision`` — NULL initially; filled by the conversion-
        assistant UI in commit #5 (e.g. ``upgraded_to_recurring`` /
        ``kept_one_shot`` / ``edited_manually``).
      * ``notes`` — one-line human-readable explanation.

    Migration: alembic 0054.
    """

    __tablename__ = "life_events_migration_log"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    original_life_event_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("life_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    original_kind: Mapped[str] = mapped_column(Text, nullable=False)
    original_amount_usd: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    target_delta_kind: Mapped[str] = mapped_column(Text, nullable=False)
    conversion_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    user_decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_life_events_migration_log_event",
            "original_life_event_id",
        ),
        # Idempotency floor — codex IMPORTANT (Spec D commit #1 review):
        # at most one log row per source life_event.id.
        UniqueConstraint(
            "original_life_event_id",
            name="uq_life_events_migration_log_event",
        ),
    )


class NewsSignal(Base):
    """Daily-automation pipeline record — one row per ingested item.

    Stage 1 (deterministic extractor — no LLM, regex + keyword) fills:
      source / source_ref / received_at / parsed_tickers / event_keywords
      / sentiment / source_trust / evidence_excerpt / raw_text

    Stage 2 (analyst LLM, Opus per accuracy-over-cost) fills:
      materiality / recommended_flag / rationale / analyzed_at

    Codex BLOCKER #2 isolation contract: raw_text is stored for citation
    display only. The Stage 2 prompt sees ONLY the normalized fields
    (parsed_tickers / event_keywords / sentiment / source_trust /
    evidence_excerpt). raw_text never reaches the LLM context.

    Migration: alembic 0043.
    """

    __tablename__ = "news_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    parsed_tickers: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    event_keywords: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    sentiment: Mapped[str] = mapped_column(String(16), nullable=False)
    source_trust: Mapped[str] = mapped_column(String(8), nullable=False)
    evidence_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    materiality: Mapped[str | None] = mapped_column(String(8), nullable=True)
    recommended_flag: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_news_signals_source_ref",
            "source",
            "source_ref",
            unique=True,
        ),
        Index("ix_news_signals_received", "received_at"),
        Index(
            "ix_news_signals_materiality_high",
            "received_at",
            sqlite_where=_sa_text("materiality = 'high'"),
            postgresql_where=_sa_text("materiality = 'high'"),
        ),
    )


class MonitorFlag(Base):
    """Active red-flag surface on /home.

    Written by the monitor agent (one of three triggers — allocation
    drift / mc regression / macro shift). Read by the Red-Flag Strip
    component on the home page. acknowledged_at tracks user dismissal;
    expires_at lets the system auto-supersede a flag (e.g. drift that
    re-fires next snapshot — emit a new row, expire the old).

    payload is JSON-encoded TEXT with kind-specific detail:
      allocation_drift: {"snapshot_date": "2026-05-29", "row": "Growth",
                         "rel_drift": 0.14, "abs_drift_usd": 8200}
      mc_regression:    {"prev_p_solvent": 0.82, "curr_p_solvent": 0.76,
                         "delta_pp": -6}
      macro_shift:      {"news_signal_id": 423, "trigger": "rate_cycle",
                         "classifier_rationale": "..."}

    Migration: alembic 0043.
    """

    __tablename__ = "monitor_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    surfaced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # Spec B (state-observer) commit #1 / migration 0049 — observer flag
    # idempotency key. NULL for legacy (allocation_drift / mc_regression
    # / macro_shift) rows; populated by the state-observer flag-writer
    # with ``v1|state_observer|<user>|<inferred_kind>|<primary_field>|<bucket>``
    # per spec §4.2. Uniqueness is enforced at the DB level only via the
    # partial unique index ``ix_monitor_flags_observer_dedup`` — declared
    # in the migration, NOT here, because it's a partial WHERE-clause
    # index that SQLAlchemy's column-level ``index=True`` cannot express.
    dedup_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lifecycle status (migration 0072). One of ``active`` / ``superseded`` /
    # ``acknowledged``:
    #   * ``active``       — the flag is live and surfaces on the Red-Flag Strip.
    #   * ``superseded``   — a later run of the SAME producer (state_observer /
    #                        thesis_monitor / plan-promotion) replaced this
    #                        observation; it no longer reflects current state.
    #   * ``acknowledged`` — the user dismissed it from the strip (kept in sync
    #                        with ``acknowledged_at`` for backward compat).
    # The /monitor/flags query returns ONLY ``status='active'`` rows. Prior to
    # 0072 the strip filtered on ``acknowledged_at IS NULL`` alone, which let
    # stale cross-run observations accumulate; ``status`` gives the producer a
    # supersede primitive so a fresh run REPLACES its prior set.
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active", nullable=False
    )

    __table_args__ = (
        Index(
            "ix_monitor_flags_user_active",
            "user_id",
            "surfaced_at",
            sqlite_where=_sa_text("acknowledged_at IS NULL"),
            postgresql_where=_sa_text("acknowledged_at IS NULL"),
        ),
    )


class PlanActionAck(Base):
    """User acknowledgement ("mark done") of a plan action-item checklist row.

    Backs the /proposals "What's on you to do" checklist completion. Each row
    records that ``user_id`` marked the action ``item_id`` done while it had a
    specific ``content_fingerprint`` — a stable hash of the item's meaningful
    content (id + label + dated + any amount in the label/detail), computed in
    ``_collect_action_items``.

    Resurface-on-change semantic: an item shows as acknowledged only when an ack
    row exists for ``(user_id, item_id)`` AND its ``content_fingerprint``
    matches the freshly computed one. If the plan later changes the item, the
    fingerprint changes, the match fails, and the item RESURFACES as not-done.
    Marking it done again upserts the row with the new fingerprint.

    ``(user_id, item_id)`` is UNIQUE so an upsert replaces the prior ack rather
    than accumulating stale fingerprints.

    Migration: alembic 0073.
    """

    __tablename__ = "plan_action_acks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    acknowledged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "item_id", name="uq_plan_action_acks_user_item"
        ),
    )


class PayslipFactRow(Base):
    """Parsed Hilan payslip facts + §102-equity withholding verdict, per period.

    Backs the closed-loop "is my RSU (§102 equity) withholding adequate?"
    feature. One row per ``(user_id, period_year, period_month)``:

    * ``source_file_id`` / ``source_sha256`` — the ``user_files`` catalog row the
      raw PDF bytes were stored under (every payslip flows through
      ``argosy/services/file_catalog.py::catalog_upload``). ``source_sha256`` is
      the idempotency key — re-ingesting identical bytes updates in place; a
      changed PDF for the same period (new sha) overwrites the row.
    * ``parsed_json`` — serialized :class:`argosy.services.payslip_parser.PayslipFacts`.
    * ``verdict_json`` — serialized
      :class:`argosy.services.rsu_reconciliation.withholding_check.WithholdingVerdict`
      so the latest verdict reads fast without re-parsing the PDF.

    ``(user_id, period_year, period_month)`` is UNIQUE so re-ingest upserts
    rather than accumulating duplicate periods.

    Migration: alembic 0074.
    """

    __tablename__ = "payslip_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_month: Mapped[int] = mapped_column(Integer, nullable=False)
    source_file_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user_files.id", ondelete="SET NULL"), nullable=True
    )
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parsed_json: Mapped[str] = mapped_column(Text, nullable=False)
    verdict_json: Mapped[str] = mapped_column(Text, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "period_year",
            "period_month",
            name="uq_payslip_facts_user_period",
        ),
    )


class StateSnapshot(Base):
    """One per-user per-day snapshot of the user's full ``current_state``.

    Spec B (state-observer) §1.2 + Appendix A — written by
    ``argosy.services.state_snapshot.persist_state_snapshot`` (spec
    commit #2). Six top-level JSON sections live inside ``state_json``:
    ``plan_inputs`` / ``portfolio`` / ``macro`` / ``cashflow_recent``
    / ``tax_assumptions`` / ``metadata``. ``source_versions_json``
    captures the adapter versions + ``as_of`` timestamps + any
    ``historical_replay_gaps`` (§1.4) the collector recorded.

    Idempotency: ``(user_id, snapshot_date)`` is UNIQUE at the DB
    level — the daily cron + on-demand triggers from §7.3 can't double-
    write the same calendar day. ``json_valid(state_json)`` and
    ``json_valid(source_versions_json)`` CHECKs (declared in the
    migration, not the model) fail corrupted writes at write time
    rather than at observer-input-assembly time.

    Migration: alembic 0049.
    """

    __tablename__ = "state_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_versions_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "snapshot_date",
            name="uq_state_snapshots_user_date",
        ),
    )


class AlphaReportAnalysis(Base):
    """LLM-structured analysis of one long-form Discord alpha report.

    Stage 2 of the Discord alpha-report pipeline: the deterministic
    ``extract_alpha_call_from_text`` regex handles tight messages
    (``BUY $NVDA target $150 stop $130``); long-form posts
    (Meet Kevin "Morning Brief" / multi-page commentary > 500 chars or
    > 5 newlines) are skipped by the regex path and instead consumed
    by the ``alpha_report_analyst`` Opus agent which extracts:

      * Macro tone + tone confidence (drives state.macro.recent_news_summary;
        the state_observer reads it as ONE input among many — no hardcoded
        "3 bearish reports → flag" detector per
        ``feedback_emergent_anomaly_detection``).
      * Per-ticker signals (sentiment + conviction + timeframe + action
        hint) → fan out to ``predictions`` rows with
        ``source='discord_alpha_report'``.
      * Structural picks (long_term_basket / rate_play / AI_play /
        defensive / speculative / other) → fan out to ``predictions``
        rows with long-bias + 180-day timeframe.
      * Cautions (free-form short warnings) — recorded here; ONLY
        cautions with a severity-warning hint promote to a MonitorFlag
        with ``kind='alpha_report_caution'``.
      * Index targets ({"QQQ": 738.5, "SPX": 5800.0}) — recorded for
        future state-observer divergence checks.

    Idempotency: ``UniqueConstraint(news_signal_id)`` enforces "at most
    one analysis per NewsSignal" at the DB layer. The runner SELECTs
    first and returns the existing row on re-runs (so downstream
    Prediction writes don't double-fire either — the per-source dedup
    keys in ``predictions.message_id`` are the second-layer guard).

    Replay safety: ``agent_version`` defaults ``'v1'`` and is bumped
    when the prompt / output schema evolves. Historical rows keep
    their original version so a v2 consumer can fall back to v1
    parsing when reading old rows.

    CHECK constraints (declared in migration 0058, not here, per the
    ``StateSnapshot`` precedent):
      * ``macro_tone`` IN ('bullish', 'cautiously_bullish', 'mixed',
        'cautiously_bearish', 'bearish').
      * ``macro_tone_confidence`` / ``confidence_overall`` IN
        ('low', 'medium', 'high').
      * ``json_valid()`` on each of the five JSON columns.

    Migration: alembic 0058.
    """

    __tablename__ = "alpha_report_analyses"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    news_signal_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("news_signals.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # CHECK enums in migration 0058.
    macro_tone: Mapped[str] = mapped_column(Text, nullable=False)
    macro_tone_confidence: Mapped[str] = mapped_column(
        Text, nullable=False
    )
    # JSON list of theme tags (e.g. ["AI cycle", "rate cuts", "tariffs"]).
    key_themes: Mapped[str] = mapped_column(Text, nullable=False)
    # 2-3 sentence rationale from the LLM.
    summary_rationale: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON list of TickerSignal dicts.
    ticker_signals_json: Mapped[str] = mapped_column(
        Text, nullable=False
    )
    # JSON list of StructuralPick dicts.
    structural_picks_json: Mapped[str] = mapped_column(
        Text, nullable=False
    )
    # JSON list of short caution strings.
    cautions_json: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON map {symbol: float}, e.g. {"QQQ": 738.5, "SPX": 5800.0}.
    index_targets_json: Mapped[str] = mapped_column(
        Text, nullable=False
    )
    confidence_overall: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=_sa_text("'v1'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint(
            "news_signal_id",
            name="uq_alpha_report_analyses_news_signal_id",
        ),
    )


class RsuVestEvent(Base):
    """Historical RSU vest event extracted from Schwab Equity Awards CSV.

    One row per restriction-lapse event. Sourced from `Lapse` action rows
    in the CSV; the paired `Deposit` row (typically T+1) is ignored as
    redundant (same AwardId + share count + FMV).

    The "upcoming vests" view that pre-vest planning needs is computed by
    PROJECTING from the historical cadence per `grant_id` (typically
    quarterly equal-tranche), not persisted to a separate table. The
    spec was revised from `rsu_unvested_grants` → `rsu_vest_events` once
    the real CSV showed no future-vest rows.

    Migration: alembic 0044.
    """

    __tablename__ = "rsu_vest_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    grant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    vest_date: Mapped[date] = mapped_column(Date, nullable=False)
    shares_vested: Mapped[float] = mapped_column(Numeric(16, 4), nullable=False)
    shares_withheld: Mapped[float] = mapped_column(Numeric(16, 4), nullable=False)
    shares_net: Mapped[float] = mapped_column(Numeric(16, 4), nullable=False)
    fmv_per_share_usd: Mapped[float] = mapped_column(
        Numeric(12, 4), nullable=False
    )
    award_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "grant_id",
            "vest_date",
            name="uq_rsu_vest_events_user_grant_date",
        ),
        CheckConstraint(
            "shares_vested >= 0", name="ck_rsu_vest_shares_nonneg"
        ),
        CheckConstraint(
            "shares_withheld >= 0", name="ck_rsu_vest_withheld_nonneg"
        ),
        CheckConstraint(
            "shares_net >= 0", name="ck_rsu_vest_net_nonneg"
        ),
        CheckConstraint(
            "fmv_per_share_usd > 0", name="ck_rsu_vest_fmv_positive"
        ),
        CheckConstraint(
            "shares_withheld <= shares_vested",
            name="ck_rsu_vest_withheld_le_vested",
        ),
        Index("ix_rsu_vest_events_user_date", "user_id", "vest_date"),
        Index("ix_rsu_vest_events_grant", "user_id", "grant_id"),
    )


class MerchantRollingStats(Base):
    """Per-merchant + per-category rolling statistics over a trailing window.

    Backs Bucket A anomaly detection (amount outliers) per spec #2 §1.1.
    Populated by the nightly recompute service
    ``argosy/services/anomaly/rolling_stats.py::recompute_merchant_stats``.

    Stats per (user_id, merchant_normalized, category_id) over a trailing
    window (default 180 days):
      * median + MAD — robust statistics, insensitive to outliers. Used
        by Pattern A1 (category robust outlier) for the z-score.
      * mean + stdev — kept for dashboard backward-compat
        (``expense_dashboard.py`` already reads them) and for Pattern A2
        (merchant spike) which compares against the mean.
      * min/max/first_seen/last_seen — descriptive context.

    ``mad_nis`` and ``stdev_nis`` are NULL when txn_count < 2 — no
    meaningful spread estimate with a single observation.

    UNIQUE (user_id, merchant_normalized, category_id, window_end) keeps
    the nightly recompute idempotent (UPSERT-keyed on the window end).

    Migration: alembic 0045.
    """

    __tablename__ = "merchant_rolling_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    merchant_normalized: Mapped[str] = mapped_column(String(512), nullable=False)
    category_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("expense_categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)
    txn_count: Mapped[int] = mapped_column(Integer, nullable=False)
    median_nis: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    mad_nis: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    mean_nis: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    stdev_nis: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    min_nis: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    max_nis: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    first_seen_at: Mapped[date] = mapped_column(Date, nullable=False)
    last_seen_at: Mapped[date] = mapped_column(Date, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "merchant_normalized",
            "category_id",
            "window_end",
            name="uq_merchant_rolling_stats_window",
        ),
        CheckConstraint(
            "txn_count >= 1",
            name="ck_merchant_rolling_stats_count_positive",
        ),
        CheckConstraint(
            "window_end >= window_start",
            name="ck_merchant_rolling_stats_window_order",
        ),
        CheckConstraint(
            "max_nis >= min_nis",
            name="ck_merchant_rolling_stats_max_ge_min",
        ),
        Index(
            "ix_merchant_rolling_stats_user_merchant",
            "user_id",
            "merchant_normalized",
        ),
    )


class WatchlistObservation(Base):
    """Per-statement observation log for a declared watchlist entry.

    Backs Pattern B1 (fee-waiver / promotion missing) per spec #2 §1.2.
    Status is a 4-state machine — see ``argosy/services/anomaly/state_tracker.py``
    for the MATCHED→MISSING transition rule that fires the flag.

    Status semantics:
      * ``MATCHED``   — statement present, both charge + discount found
      * ``MISSING``   — statement present, charge found but discount missing
      * ``PARTIAL``   — statement present, charge missing entirely
      * ``UNKNOWN``   — statement absent for the observation period

    ``evidence_tx_ids`` is JSON-encoded list of expense_transactions.id
    rows that contributed to the observation (used for citation display).

    Migration: alembic 0046.
    """

    __tablename__ = "watchlist_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    watchlist_entry_id: Mapped[str] = mapped_column(String(128), nullable=False)
    observation_period: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_tx_ids: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "watchlist_entry_id",
            "observation_period",
            name="uq_watchlist_observations_period",
        ),
        CheckConstraint(
            "status IN ('MATCHED', 'MISSING', 'PARTIAL', 'UNKNOWN')",
            name="ck_watchlist_observations_status",
        ),
        Index(
            "ix_watchlist_observations_user_entry",
            "user_id",
            "watchlist_entry_id",
            "observation_period",
        ),
    )


class RecurringChargePattern(Base):
    """Learned recurring-charge pattern for a (user, merchant) pair.

    Backs Pattern B2 (recurring-charge missing) per spec #2 §1.2.
    Learner requires ≥3 occurrences at roughly the same amount (±15%) on
    roughly monthly cadence before a pattern goes ``active``.

    ``status``:
      * ``active``         — pattern is being monitored; missing-charge fires
      * ``dormant``        — pattern hasn't fired in a long time but kept for history
      * ``user_dismissed`` — user told us to stop monitoring

    Detector fires when ``last_seen + cadence_days + cadence_tolerance_days``
    has passed with no fresh match.

    Migration: alembic 0046.
    """

    __tablename__ = "recurring_charge_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    merchant_normalized: Mapped[str] = mapped_column(String(512), nullable=False)
    expected_amount_nis: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    amount_tolerance: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=Decimal("0.15")
    )
    cadence_days: Mapped[int] = mapped_column(Integer, nullable=False)
    cadence_tolerance_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7
    )
    first_seen: Mapped[date] = mapped_column(Date, nullable=False)
    last_seen: Mapped[date] = mapped_column(Date, nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active"
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "merchant_normalized",
            "expected_amount_nis",
            name="uq_recurring_charge_patterns_merchant",
        ),
        CheckConstraint(
            "status IN ('active', 'dormant', 'user_dismissed')",
            name="ck_recurring_charge_patterns_status",
        ),
        CheckConstraint(
            "expected_amount_nis > 0",
            name="ck_recurring_charge_patterns_amount_positive",
        ),
        CheckConstraint(
            "cadence_days > 0",
            name="ck_recurring_charge_patterns_cadence_positive",
        ),
        CheckConstraint(
            "occurrence_count >= 3",
            name="ck_recurring_charge_patterns_min_occurrences",
        ),
        Index(
            "ix_recurring_charge_patterns_user_merchant",
            "user_id",
            "merchant_normalized",
        ),
        Index(
            "ix_recurring_charge_patterns_active",
            "user_id",
            "last_seen",
            sqlite_where=_sa_text("status = 'active'"),
            postgresql_where=_sa_text("status = 'active'"),
        ),
    )


class PortfolioSnapshotPart(Base):
    """Pending half of a Leumi monthly portfolio snapshot.

    A Leumi monthly XLS export carries positions but no cash — cash must
    come from the user's Leumi Osh (current-account) statement. When the
    XLS uploads:
      * If a matching Osh statement is already in expense_statements
        within the +/-15d match window, assembly fires immediately and
        the row is created with status='resolved'.
      * Otherwise the row is created with status='pending'; the Osh-side
        hook (try_resolve_pending_on_osh_arrival) picks it up when the
        paired Osh statement subsequently lands.

    Idempotency layers (both enforced by table_args UniqueConstraint):
      * sha256 of XLS bytes  -- fast-path "same file re-uploaded".
      * (snapshot_date, portfolio_number) -- semantic dedup. Same data
        re-exported with different XML byte layout still resolves to
        the same row. Codex zigzag finding #9 (2026-05-29).

    See ``argosy.services.portfolio_ingest.xls_osh_pair`` for the
    assembly + TSV splice logic.
    """

    __tablename__ = "portfolio_snapshot_parts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # v1 has one kind ("xls_positions"); leaving room for future
    # part kinds (e.g. "schwab_csv_positions") without a migration.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    portfolio_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    paired_osh_statement_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("expense_statements.id", ondelete="SET NULL"),
        nullable=True,
    )
    paired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_tsv_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "sha256",
            name="uq_portfolio_snapshot_parts_user_sha",
        ),
        # Semantic dedup: partial unique index limited to rows with a
        # non-NULL portfolio_number. SQLite treats each NULL as distinct
        # under a full UNIQUE constraint, so the prior table-level
        # UniqueConstraint didn't actually enforce anything for rows
        # missing the Leumi portfolio number (codex zigzag (a)#8,
        # 2026-05-29). Migration 0040 replaces it with the partial index.
        Index(
            "ix_portfolio_snapshot_parts_user_date_portfolio_nonnull",
            "user_id", "snapshot_date", "portfolio_number",
            unique=True,
            sqlite_where=_sa_text("portfolio_number IS NOT NULL"),
            postgresql_where=_sa_text("portfolio_number IS NOT NULL"),
        ),
        Index(
            "ix_portfolio_snapshot_parts_user_status_date",
            "user_id", "status", "snapshot_date",
        ),
    )


# ----------------------------------------------------------------------
# Spec C (predictions-ledger) — predictions table (migration 0050)
# ----------------------------------------------------------------------


class Prediction(Base):
    """One row per signal-source prediction.

    Spec C (predictions-ledger) §1.2 + Appendix A — written by the
    per-source writer adapters in
    ``argosy/services/predictions/writers.py`` (commit #3). Every
    prediction collapses to the same four-field core (ticker, direction,
    timeframe, optional levels); source-specific extras live in
    ``source_ref`` (JSON-shaped TEXT, per-source caller-defined).

    Two timestamps with distinct semantics — codex IMPORTANT 2 fix in
    spec §2.3:

    * ``event_at`` — real-world prediction time (Discord msg ts, filing
      date, news publish time, internal-agent emit time). The
      ENTRY-PRICE snapshot anchors at this moment; the EVALUATION
      WINDOW starts at this moment. Writers MUST pass it explicitly so
      backfill (14d-old event_at, ``CURRENT_TIMESTAMP`` created_at)
      keeps reproducing the same outcome.
    * ``created_at`` — DB insertion timestamp; audit-only. Scoring math
      never touches it.

    ``evaluation_due_at`` + ``evaluation_method`` are pre-computed at
    write time per spec §3.1 (codex BLOCKERs 1 + 2 fix). The
    evaluator's "what's due?" query reads ``evaluation_due_at``
    directly — no re-derivation from raw ``timeframe_days`` — so the
    §5.5 30-day cap on long-horizon predictions (e.g. 13F at 90d)
    fires correctly at 30d. ``evaluation_method`` carries the chosen
    registry method name; the FK into
    ``evaluation_method_registry`` is attached by sibling migration
    0051. CHECK constraints (source / direction enums,
    ``timeframe_days > 0``, JSON validity, ``archived`` bool) are
    declared in the migration, not here, to match the precedent set
    by ``StateSnapshot``.

    Indexes — declared in the migration:

    * ``ix_predictions_source_event`` on ``(source, event_at DESC)``
      — per-source rollups, newest-first.
    * ``ix_predictions_ticker_event`` on ``(ticker, event_at DESC)``
      partial ``WHERE ticker IS NOT NULL`` — per-ticker historical
      lookup.
    * ``ix_predictions_source_messageid`` UNIQUE on ``(source,
      message_id)`` partial ``WHERE message_id IS NOT NULL`` —
      per-source dedup; writer computes
      ``v1|predictions|<source>|<entity-id>`` per §2.2.
    * ``ix_predictions_due_at`` on ``(evaluation_due_at)`` partial
      ``WHERE archived = 0`` — evaluator hot-path.
    * ``ix_predictions_event_at`` on ``(event_at)`` — time-ordered
      scan for backfill cursors and the rolling-window reliability
      view.

    Migration: alembic 0050.
    """

    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Spec §1.2 — 11 v1 enum values; CHECK declared in migration.
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON-shaped TEXT; per-source caller-defined shape (Discord:
    # {"channel_id":..., "message_id":...}; 13F: {"filing_id":...};
    # etc.). NOT NULL so every row traces back to its origin.
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL for multi-ticker baskets (read multi_ticker_json) or macro
    # predictions (no single ticker).
    ticker: Mapped[str | None] = mapped_column(Text, nullable=True)
    # CHECK enum: long / short / neutral / multi. neutral covers HOLD
    # verdicts + qualitative flags (codex BLOCKER 3 fix in §2.4).
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    target_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    stop_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    # NULL falls back to per-source default in §1.2.
    timeframe_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # JSON list of basket constituents [{ticker, direction, weight}]
    # for direction='multi' rows; json_valid CHECK in migration.
    multi_ticker_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    # JSON map {ticker: entry_price} for the multi-ticker basket case
    # (spec §5.4); json_valid CHECK in migration.
    entry_prices_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    # Stable per-source dedup key, e.g.
    # ``v1|predictions|discord|<channel_id>.<message_id>``. NULL for
    # rows without a natural key; UNIQUE only when NOT NULL (partial
    # index in migration).
    message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Pointer to raw source content (e.g. ``news_signals.id:423`` or a
    # filing URL). NEVER injected into LLM prompts per §1.2.
    raw_text_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Writer sets this when the source has no actionable shape;
    # evaluator marks the row ``unparseable`` and excludes from
    # reliability stats but counts in coverage.
    unparseable_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    # Real-world prediction time (codex IMPORTANT 2 fix). Distinct
    # from created_at on backfill.
    event_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Audit-only insertion timestamp. Server default
    # CURRENT_TIMESTAMP; scoring math never touches it.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )
    # Pre-computed at write time as event_at + chosen_window_days
    # (codex BLOCKER 2 fix). Evaluator due-query keys off this.
    evaluation_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Plain TEXT today; sibling migration 0051 attaches the FK into
    # the ``evaluation_method_registry`` table (codex BLOCKER 1 fix).
    # No CHECK enum by design — new method versions land via INSERT
    # into the registry.
    evaluation_method: Mapped[str] = mapped_column(Text, nullable=False)
    # Retention compact flag (codex IMPORTANT 4). SQLite-native bool
    # via INTEGER 0/1 (default 0); set to 1 by the
    # ``predictions-retention-compact`` job (spec §9.1) once a row is
    # > 2y old + 90d-inactive. The partial ``ix_predictions_due_at``
    # index excludes archived rows so the evaluator scan stays bounded.
    archived: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=_sa_text("0"),
    )
    # Spec C commit #6 — anti-feedback-loop stamp (spec §6.6 / codex
    # IMPORTANT 3). SQLite-native bool via INTEGER 0/1 (default 0).
    # Set to 1 by any consumer that derives a NEW prediction row from a
    # source whose reliability weight it already applied; downstream
    # consumers see the stamp and skip re-applying the weight (return
    # 1.0 from ``get_weight_for_source``). Prevents the discord →
    # news_signal_analyst → plan_synthesizer chain from compounding
    # attenuation across three hops (0.5 × 0.5 × 0.5 = 0.125). The
    # 0.10 floor in ``get_weight_for_source`` is the safety net; this
    # stamp is the primary discipline. CHECK constraint declared in
    # migration 0053.
    provenance_weights_applied: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=_sa_text("0"),
    )


class EvaluationMethod(Base):
    """Versioned registry of outcome-evaluator scoring methods.

    Spec C (predictions ledger) §3.4 + Appendix A — Codex BLOCKER 1
    fix. The registry replaces the original CHECK enum on
    ``predictions.evaluation_method`` and
    ``prediction_outcomes.evaluation_method`` so that NEW method
    versions land via an INSERT into this table, not a schema
    migration. The source-reliability view (commit #5) joins on
    ``is_active = 1`` and picks the most-recently-evaluated active
    method per ``family`` so a single prediction never double-counts
    across method versions during a transition window.

    Seeded by migration 0051 with five v1 rows: ``target_stop``,
    ``fixed_lookahead_7d``, ``fixed_lookahead_30d``,
    ``multi_basket_weighted``, and ``unparseable`` (the
    method-of-record for unparseable predictions).

    Retiring a method = UPDATE ``is_active = 0`` +
    ``superseded_by = <new_method_name>``. Never DELETE — historical
    rows in ``prediction_outcomes`` retain the FK pointer and the
    audit-trail "this prediction was scored by v1 of the rule" stays
    answerable.

    Migration: alembic 0051.
    """

    __tablename__ = "evaluation_method_registry"

    method_name: Mapped[str] = mapped_column(Text, primary_key=True)
    family: Mapped[str] = mapped_column(Text, nullable=False)
    method_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=_sa_text("1")
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SQLite-native bool: 0/1. ``is_active = 1`` is the source-of-truth
    # filter in the source_reliability view.
    is_active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=_sa_text("1")
    )
    superseded_by: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "evaluation_method_registry.method_name",
            name="fk_eval_method_registry_superseded_by",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        CheckConstraint(
            "is_active IN (0, 1)",
            name="ck_eval_method_registry_is_active_bool",
        ),
        CheckConstraint(
            "method_version >= 1",
            name="ck_eval_method_registry_version_positive",
        ),
    )


class PredictionOutcome(Base):
    """One row per evaluated prediction (per evaluation_method).

    Spec C (predictions ledger) §1.3 + Appendix A. Written by the
    outcome evaluator (commit #4) using ``INSERT ... ON CONFLICT
    (prediction_id, evaluation_method) DO NOTHING`` for idempotent
    re-runs.

    Six ``outcome_kind`` values per §2.4: ``hit_target`` /
    ``hit_stop`` / ``expired_neutral`` / ``expired_positive`` /
    ``expired_negative`` / ``unparseable``. CHECK constraint (declared
    in the migration) enforces.

    ``pnl_pct`` is signed against the prediction's direction — positive
    means the prediction was right. NULL for ``unparseable`` rows and
    for rows where price data was missing entirely.

    Replay-safety: ``(prediction_id, evaluation_method)`` is UNIQUE
    (the natural key). Re-scoring the same prediction under the same
    method is a no-op; the evaluator switches to a NEW
    ``evaluation_method`` row when the rule changes, leaving the prior
    outcome row intact.

    FK ``prediction_id → predictions.id ON DELETE CASCADE`` — retention
    archival of a prediction takes its outcomes with it.

    Migration: alembic 0051.
    """

    __tablename__ = "prediction_outcomes"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    prediction_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "predictions.id",
            ondelete="CASCADE",
            name="fk_prediction_outcomes_prediction_id",
        ),
        nullable=False,
    )
    outcome_kind: Mapped[str] = mapped_column(Text, nullable=False)
    pnl_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 4), nullable=True
    )
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )
    evaluation_method: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "evaluation_method_registry.method_name",
            name="fk_prediction_outcomes_evaluation_method",
        ),
        nullable=False,
    )
    entry_price_used: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    exit_price_used: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    exit_trigger_date: Mapped[date | None] = mapped_column(
        Date, nullable=True
    )
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "prediction_id",
            "evaluation_method",
            name="ix_outcomes_pred_method",
        ),
        Index("ix_outcomes_evaluated", "evaluated_at"),
        Index("ix_outcomes_kind", "outcome_kind"),
    )


class ActionProposal(Base):
    """One row per system-proposed action (the action ledger).

    Spec E (last-mile delivery) §1 + Appendix A — written by the
    action_proposer agent (commit #2) and the inferred-life-event
    detector (commit #5).  The proposer NEVER executes; it RECORDS.
    The user reviews via the /proposals UI (commit #6) and clicks
    Accept / Defer / Reject / Customize.

    The ``execution_state`` column is the structural capability-
    boundary enforcement (codex BLOCKER #1 / spec §2.2.1).  Three-value
    enum tracking SUGGESTION -> USER ACTION transitions only:

    * ``proposed`` — default; every proposer write starts here.
    * ``accepted_pending_user_action`` — Accept handler set this;
      money has NOT moved; the row is queued for the existing
      ``proposals -> action_engine -> orders`` pipeline which has its
      own user-confirmation gates.
    * ``dismissed`` — terminal state for rejected / expired proposals.

    There is NO auto-execute path in this codebase.  The column gates
    UI transitions; capability-boundary enforcement at the Accept
    handlers (test_action_proposal_no_execution_invariant.py in commit
    #2) is the runtime mirror.

    Dedup contract (spec §1.5): the partial UNIQUE index
    ``ix_action_proposals_dedup_open`` enforces "one open proposal per
    (user_id, dedup_key)" only while ``status='open'`` AND
    ``dedup_key IS NOT NULL``.  A proposal transitioning out of 'open'
    releases its dedup_key for re-firing — write-orchestrated tombstone
    pattern matching Spec B's pattern from migration 0049.

    FKs:
      * ``user_id`` -> users.id ON DELETE CASCADE.
      * ``source_flag_id`` -> monitor_flags.id ON DELETE SET NULL.
        Losing the source flag (housekeeping sweep) must NOT cascade
        away an already-surfaced proposal.
      * ``source_observation_id`` -> state_snapshots.id ON DELETE
        SET NULL.  Same reasoning.
      * ``source_inferred_event_id`` — plain INTEGER (NO FK
        constraint); the inferred_life_event_findings table lands in
        commit #5.  Writer in commit #5 populates this.

    Migration: alembic 0055.
    """

    __tablename__ = "action_proposals"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_flag_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("monitor_flags.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_observation_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("state_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_inferred_event_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    rationale_md: Mapped[str] = mapped_column(Text, nullable=False)
    # Structured payload per the kind's Pydantic schema (spec §1.4).
    # json_valid CHECK declared in the migration.
    suggested_payload: Mapped[str] = mapped_column(Text, nullable=False)
    # CHECK enum: info / warning / critical. Declared in migration.
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    surfaced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )
    # NOT NULL: writer computes surfaced_at + 7d (critical) / 30d
    # (non-critical) cushion per spec §1.6.  Housekeeping loop
    # transitions expires_at-passed rows out of status='open'.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # CHECK enum: open / accepted / deferred / rejected / superseded.
    # Default 'open' via server_default.  Declared in migration.
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=_sa_text("'open'"),
        default="open",
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_by_user_note: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    # CHECK enum: 8 v1 kinds (allocate / repatriate_currency /
    # rebalance / replan_full / add_life_event_phase /
    # update_plan_assumption / set_watchlist / note_only). Declared
    # in migration.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Spec §1.5 tombstone-pattern dedup key. NULLABLE — proposals
    # without a deterministic dedup contract fall outside the
    # uniqueness scope. Partial UNIQUE index declared in migration.
    dedup_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Codex BLOCKER #1 capability-boundary column (spec §2.2.1).
    # CHECK enum: proposed / accepted_pending_user_action / dismissed.
    # Default 'proposed' via server_default. Declared in migration.
    execution_state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=_sa_text("'proposed'"),
        default="proposed",
    )


class NotificationSubscription(Base):
    """One row per (user, channel, endpoint) push subscription.

    Spec E §3.4 + Appendix A — written by the
    ``POST /api/notifications/subscriptions`` route on UI opt-in
    (commit #7).  The notification_dispatcher (commit #3) reads
    ``status='active'`` rows and fans out per-channel.

    Channels (CHECK enforced in migration):

    * ``web_push`` — VAPID-signed POST to ``endpoint`` (browser-vendor
      push service URL).  ``p256dh`` + ``auth`` carry the browser's
      public key + auth secret captured via ``pushManager.subscribe``.
    * ``email`` — ``endpoint`` is the recipient address; ``p256dh`` +
      ``auth`` are NULL.
    * ``in_app`` — ``endpoint`` is the WebSocket channel id; ``p256dh``
      + ``auth`` are NULL.

    The "p256dh + auth required when channel=web_push" invariant is
    enforced at the application layer (NOT a DB constraint) per spec
    §3.4 / codex review — too many edge cases (subscription
    re-issuance, browser-vendor URL evolution) to pin at DDL time
    without breaking legitimate writes.

    Status lifecycle: ``active`` -> ``gone`` (set when the push
    endpoint returns HTTP 410: browser uninstalled the service worker
    or user revoked permission).  Gone rows are skipped by the
    dispatcher; the partial index ``ix_notification_subscriptions_
    user_active`` keeps active-only lookup bounded.

    UNIQUE(user_id, channel, endpoint) prevents duplicate
    subscriptions for the same channel-endpoint pair (browser may
    re-POST on each reload; the writer treats UNIQUE-violation as
    "already subscribed, no-op").

    Migration: alembic 0055.
    """

    __tablename__ = "notification_subscriptions"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # CHECK enum: web_push / email / in_app. Declared in migration.
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    # Web-push crypto material — nullable for non-web_push channels.
    p256dh: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth: Mapped[str | None] = mapped_column(Text, nullable=True)
    subscribed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # CHECK enum: active / gone. Default 'active'.  Declared in
    # migration.
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=_sa_text("'active'"),
        default="active",
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "channel",
            "endpoint",
            name="uq_notification_subscriptions_user_channel_endpoint",
        ),
    )


class NotificationPreference(Base):
    """Per-(user, channel, severity, kind) enable matrix.

    Spec E §3.3 + Appendix A — one row per cell in the
    channel x severity x kind enable cube.  ``enabled=1`` means the
    notification_dispatcher (commit #3) will fan out matching
    notifications on this channel; ``enabled=0`` mutes the cell.

    Defaults are seeded at user creation (commit #3 wiring): see
    spec §3.3 table — in_app/info default ON, web_push/info default
    OFF (push is interruption-grade, warning is the floor), etc.

    ``kind`` is permissive TEXT (NO CHECK enum) so adding a new
    MonitorFlag.kind family or action_proposal.kind doesn't require
    a CHECK-relaxation migration.  The writer contract accepts any
    string starting with ``state_observer_`` / ``drift`` / etc. plus
    the eight action_proposal kinds.

    UNIQUE(user_id, channel, severity, kind) is the natural key — one
    row per cell.  The dispatcher's preference-lookup query keys off
    this UNIQUE constraint via an upsert pattern.

    Migration: alembic 0055.
    """

    __tablename__ = "notification_preferences"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # CHECK enum: web_push / email / in_app. Declared in migration.
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    # CHECK enum: info / warning / critical. Declared in migration.
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    # Permissive TEXT — no CHECK enum (see class docstring).
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # SQLite-native bool via INTEGER 0/1. CHECK enforces (0, 1) in
    # the migration.
    enabled: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=_sa_text("1"),
        default=1,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "channel",
            "severity",
            "kind",
            name="uq_notification_preferences_user_cell",
        ),
    )


class NotificationDispatchLedger(Base):
    """One row per dispatch attempt (audit + idempotency).

    Spec E §3.6 + Appendix A — written by the notification_dispatcher
    (commit #3) on every fan-out attempt.  ``notification_id`` is the
    deterministic cross-channel dedup key (writer convention:
    ``f"{kind}|{ref_id}|{channel}|{severity}|{utc_day}"``);
    UNIQUE(user_id, notification_id, channel) enforces idempotent
    re-dispatch at the DB layer — a retry attempt for the same
    notification on the same channel is rejected even if the
    application-level dedup check missed.  The uniqueness scope
    INCLUDES ``user_id`` per codex BLOCKER (Spec E #1 review) — the
    writer's deterministic notification_id isn't user-namespaced, so
    two tenants would otherwise collide on identical ids.

    Statuses (CHECK enforced in migration):

    * ``sent`` — channel returned 2xx; ``error_message`` is NULL.
    * ``failed`` — channel returned non-2xx; ``error_message`` carries
      the diagnostic.
    * ``skipped`` — preference matrix or dedup gate rejected before
      attempt; ``error_message`` may carry the skip reason.

    ``subscription_id`` FK -> notification_subscriptions ON DELETE
    SET NULL.  Losing the subscription (user opted out) must NOT
    cascade away the dispatch audit row.

    Migration: alembic 0055.
    """

    __tablename__ = "notification_dispatch_ledger"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    notification_id: Mapped[str] = mapped_column(Text, nullable=False)
    # CHECK enum: web_push / email / in_app. Declared in migration.
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    subscription_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "notification_subscriptions.id", ondelete="SET NULL"
        ),
        nullable=True,
    )
    dispatched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )
    # CHECK enum: sent / failed / skipped. Declared in migration.
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "notification_id",
            "channel",
            name="uq_notification_dispatch_ledger_user_notification_channel",
        ),
    )


class ReplanDispatchLog(Base):
    """One row per observer->replan dispatch decision (Spec E commit #4).

    Spec E §4 + Appendix A.8 — written by
    ``argosy/services/replan_dispatcher.py::maybe_dispatch_replan`` on
    every flag arrival that the state-observer flag-writer routes
    through the dispatcher (severity >= warning).  Every gate decision
    produces a row regardless of outcome so the admin UI can audit
    "why did the system not replan when FX shifted again?"

    Outcomes (CHECK enforced in migration 0056):

    * ``fired`` — all gates clear; ``JobRegistry.fire_now`` was called;
      ``job_run_id`` is the audit row id from ``job_runs``.
    * ``dry_run_logged`` — flag was mapped + severity below the
      per-trigger threshold; the row exists so the operator can audit
      warning-band observer fires that mapped to a replan kind but
      were filtered out (spec §4.2 "warning is dry-run-logged").
    * ``skipped_cooldown`` — same (user, trigger_kind) fired within
      the per-kind cooldown window (default 72h; spec §4.3 Gate 1).
    * ``skipped_global_cap`` — user already has 4 ``fired`` rows in
      the last 72h regardless of trigger_kind (spec §4.3 Gate 2).
    * ``skipped_severity`` — flag's kind is not in the mapping table
      OR the severity didn't meet the trigger's minimum.
    * ``error`` — ``JobRegistry.fire_now`` raised; the row may carry
      job_run_id=NULL even though the dispatcher's intent was 'fired'.
      See ``maybe_dispatch_replan`` for the idempotency-on-retry flip
      logic.

    FKs:
      * ``user_id`` -> users.id ON DELETE CASCADE.
      * ``source_flag_id`` -> monitor_flags.id ON DELETE SET NULL.
        Losing the source flag (housekeeping sweep) must NOT cascade
        away the dispatch audit row.
      * ``job_run_id`` -> job_runs.id ON DELETE SET NULL.  Losing the
        job_run (retention loop cleanup) must NOT cascade away the
        dispatch audit row.

    Migration: alembic 0056.
    """

    __tablename__ = "replan_dispatch_log"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_flag_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("monitor_flags.id", ondelete="SET NULL"),
        nullable=True,
    )
    # CHECK enum: nine kinds — seven classical replan_triggers plus
    # two synthetic observer_emergent_* kinds. Declared in migration.
    trigger_kind: Mapped[str] = mapped_column(Text, nullable=False)
    # CHECK enum: info / warning / critical. Declared in migration.
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    # CHECK enum: six outcome statuses (see class docstring). Declared
    # in migration.
    status: Mapped[str] = mapped_column(Text, nullable=False)
    job_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("job_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    dispatched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class InferredLifeEventFinding(Base):
    """One row per inferred life-event finding (Spec E commit #5 / §5.5).

    The inferred-life-event detector reads the transaction stream and
    proposes phase-change ``LifeEvent`` candidates to the user via the
    same ``action_proposals`` ledger.  The detector NEVER writes
    directly to ``life_events`` — it writes a finding here, then fires
    the action-proposer-runner which lands an ``action_proposals`` row
    with ``kind='add_life_event_phase'``.

    The two layers (heuristic + LLM disambiguator) both leave their
    audit trail here:

    * ``heuristic_confidence`` — the deterministic layer's confidence
      band (high / medium / low).  Drives whether the LLM
      disambiguator fires (spec §5.2 — medium/low go through the LLM;
      high bypasses it unless the conflict resolver flagged the
      finding).
    * ``llm_confirmed`` — the LLM disambiguator's outcome (NULL when
      the LLM was skipped; TRUE/FALSE otherwise).
    * ``dismissed`` — the final pre-proposer flag.  Any guardrail can
      flip this; once flipped, no action_proposals row is created.

    Conflict resolution (codex BLOCKER #3 / spec §5.4 guardrail #5):
    ``conflict_resolution`` records the pre-proposal conflict
    resolver's outcome.  See migration 0057 for the enum semantics.

    Idempotency contract: UNIQUE(user_id, pattern,
    evidence_window_start, evidence_window_end) — re-running the
    detector with the same window-and-pattern combination MUST NOT
    insert a duplicate finding.  See spec §5.5 / Appendix A.9.

    FKs:
      * ``user_id`` -> users.id ON DELETE CASCADE.
      * ``proposed_action_id`` -> action_proposals.id ON DELETE SET
        NULL.  Losing the proposal (housekeeping sweep) must NOT
        cascade away the finding's audit trail.

    Migration: alembic 0057.
    """

    __tablename__ = "inferred_life_event_findings"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # CHECK enum (six values; see migration 0057).  Matches
    # InferredEventTrigger.pattern literal in
    # argosy/agents/action_proposer.py.
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    # CHECK enum: high / medium / low.  Declared in migration.
    heuristic_confidence: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL = disambiguator not run (high-confidence heuristic skipped
    # the LLM).  TRUE/FALSE otherwise.
    llm_confirmed: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    dismissed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=_sa_text("0"),
    )
    evidence_window_start: Mapped[date] = mapped_column(Date, nullable=False)
    evidence_window_end: Mapped[date] = mapped_column(Date, nullable=False)
    # JSON list of expense_transactions.id (the cited evidence).
    # json_valid CHECK declared in migration.
    evidence_transaction_ids: Mapped[str] = mapped_column(
        Text, nullable=False
    )
    evidence_summary: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_action_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("action_proposals.id", ondelete="SET NULL"),
        nullable=True,
    )
    # CHECK enum (4 values + NULL).  See migration 0057.
    conflict_resolution: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "pattern",
            "evidence_window_start",
            "evidence_window_end",
            name="uq_inferred_findings_pattern_evidence",
        ),
    )


class PendingReevaluation(Base):
    """/consult auto-retry queue.

    When a /consult run lands at INSUFFICIENT_DATA (trader couldn't
    complete the analysis because load-bearing inputs were missing
    AFTER the per-ticker remediation flow exhausted its retries), the
    route persists a row here. A daily job
    (``argosy/orchestrator/loops/pending_reevaluation_daily.py``)
    sweeps the queue + re-fires the consult with the original
    parameters; on a real BUY/HOLD/SELL verdict the user is notified
    via the existing notification_dispatcher with a deep-link to the
    new run.

    CHECK constraints + UNIQUE / INDEX declared in migration 0059.
    """

    __tablename__ = "pending_reevaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    tier_value: Mapped[str] = mapped_column(String(8), nullable=False)
    consult_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    user_constraints: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'pending' (will retry tomorrow), 'resolved' (retry succeeded),
    # 'abandoned' (exceeded max attempts).  CHECK in migration 0059.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )
    last_attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )
    # FK kept lightweight (no ondelete cascade) so dropping a
    # decision_run doesn't silently null the resolution link — the
    # daily job nulls it explicitly when re-queuing.
    resolved_decision_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ScanState(Base):
    """Per-(user, ticker) discovery memory for the high-potential funnel's smart
    refresh (Phase 2). Records the last radar score + a radar fingerprint, the
    cached estimator/fleet verdicts (JSON), a status/rank/quarantine, and the
    per-stage timestamps. The funnel diffs against this to re-research only
    new/changed names; ``status`` evicts dropped tickers and quarantines bad ones.

    Migration: alembic 0066. JSON columns are Text + ``json_valid`` CHECK
    (mirrors 0049); composite PK ``(user_id, ticker)``; covering index on
    ``(user_id, status)`` for the GET path.
    """

    __tablename__ = "trend_scan_state"

    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ticker: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active",
        server_default=_sa_text("'active'"),
    )
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quarantine_reason: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=_sa_text("''"),
    )
    radar_fingerprint: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=_sa_text("''"),
    )
    estimator_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    fleet_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_radar_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_estimated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_fleet_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_sa_text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        CheckConstraint(
            "estimator_json IS NULL OR json_valid(estimator_json)",
            name="ck_trend_scan_state_estimator_json_valid",
        ),
        CheckConstraint(
            "fleet_json IS NULL OR json_valid(fleet_json)",
            name="ck_trend_scan_state_fleet_json_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'quarantined', 'dropped')",
            name="ck_trend_scan_state_status",
        ),
        Index("ix_trend_scan_state_user_status", "user_id", "status"),
    )


# ----------------------------------------------------------------------
# Phase 1c: derivation-graph persistence + replay trace
# (spec: docs/superpowers/specs/2026-06-18-living-plan-derivation-graph-design.md
#  "Data model" section). plan_nodes/plan_edges persist a DerivationGraph;
# propagation_events records the per-change blast-radius ripple for
# after-the-fact Replay. change_requests/dialogue_turns are created here so
# the one migration is complete; the negotiation ladder that writes them is
# Phase 2.
# ----------------------------------------------------------------------


class PlanNode(Base):
    """One node of a persisted DerivationGraph for a plan.

    Mirrors argosy.quality.derivation_graph.Node EXCEPT the recipe callable,
    which is code (re-attached from a recipe_registry on load), not data.
    status_validity (valid|stale) and status_flag (none|flagged) are
    ORTHOGONAL per the spec — a node can be both stale AND flagged.
    """

    __tablename__ = "plan_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_key: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # input|derived|surface
    # DERIVED/INPUT numeric or structured value, JSON-encoded. NULL for a
    # pure-prose surface (which uses `content`).
    value_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SURFACE rendered text/markup. NULL for input/derived nodes.
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status_validity: Mapped[str] = mapped_column(
        String(8), nullable=False, default="stale", server_default="stale"
    )
    status_flag: Mapped[str] = mapped_column(
        String(8), nullable=False, default="none", server_default="none"
    )
    # {recipe_key, author/source, render_template, ...}; recipe_key re-links
    # to the recipe_registry on load.
    provenance_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    owner: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    compute_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_plan_nodes_plan_key", "plan_id", "node_key", unique=True),
    )


class PlanEdge(Base):
    """A derived_from edge, materialized for query/audit. Direction is
    from_node_key (the input) -> to_node_key (the consumer that depends on it),
    i.e. to_node_key has from_node_key in its `inputs`."""

    __tablename__ = "plan_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_node_key: Mapped[str] = mapped_column(String(256), nullable=False)
    to_node_key: Mapped[str] = mapped_column(String(256), nullable=False)
    # named | set | predicate (spec: hybrid edges). Plain "named" for now.
    edge_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="named", server_default="named"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_plan_edges_plan_from_to",
            "plan_id", "from_node_key", "to_node_key", "edge_kind",
            unique=True,
        ),
    )


class ChangeRequest(Base):
    """The single author-agnostic primitive (user | agent_role) targeting one
    node. CREATED here for schema completeness; the negotiation ladder that
    populates it is Phase 2."""

    __tablename__ = "change_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_node_key: Mapped[str] = mapped_column(String(256), nullable=False)
    author: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # proposed|in_dialogue|escalated_arbiter|escalated_user|A_conceded|
    # B_conceded|arbiter_ruled|superseded
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="proposed", server_default="proposed"
    )
    round_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    adjudicated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class DialogueTurn(Base):
    """One replayable back-and-forth turn on a ChangeRequest (Layer 5.1).
    CREATED here for schema completeness; written in Phase 2."""

    __tablename__ = "dialogue_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("change_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    speaker: Mapped[str] = mapped_column(String(16), nullable=False)  # A|B|arbiter|user
    # propose|rebut|concede|rule|classify|ask|answer
    stance: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cited_nodes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


# Layer-2 (negotiation ladder) reuses the Phase-1c schema. The substrate code
# refers to these tables via the *Row aliases to disambiguate from the pure
# dataclass argosy.quality.change_adjudication.ChangeRequest.
ChangeRequestRow = ChangeRequest
DialogueTurnRow = DialogueTurn


class PropagationEvent(Base):
    """The visible blast-radius ripple for ONE applied change (Layer 5.2).
    Written by graph_store.emit_propagation_event after a propagation;
    read back by the Replay reader. trigger -> invalidated -> recomputed
    (old->new) -> rerendered surfaces -> verification verdicts."""

    __tablename__ = "propagation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Groups the propagation_events of one steady-state run for ordered replay.
    cycle_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trigger_node_key: Mapped[str] = mapped_column(String(256), nullable=False)
    invalidated_node_keys_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    # {node_key: {"old": <json-able>, "new": <json-able>}}
    recomputed_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    rerendered_surfaces_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    # {check_name: verdict_str}
    verification_verdicts_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_propagation_events_plan_cycle", "plan_id", "cycle_id"),
    )
