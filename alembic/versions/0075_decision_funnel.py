"""Decision-funnel observability + proposal lifecycle columns.

Foundation (P0) for the daily decision funnel (/proposals living surface):

- ``funnel_runs``    — one row per daily funnel execution; per-stage totals,
  the Stage-0 macro read, the policy/IPS version reasoned against, shadow
  flag, and an idempotency key (one run per user per calendar day).
- ``decision_snapshots`` — IMMUTABLE per-decision frozen state so the
  question "why did it (not) act on X today?" is answerable without a
  re-run. Captures model name/version + prompt-template hash + temp/seed,
  the EXACT portfolio + market snapshot, the decision-policy version, the
  dedup key + "unchanged" explanation, why-not-act, post-decision drift,
  and the human action state.
- ``funnel_stage_rows`` — append-only per-stage, per-name audit: every name
  considered → routed / dropped / no-op / proposed, with the signal/rule
  that fired, the model + tokens, and references to the snapshot + proposal.

Plus four columns on ``proposals`` so funnel-generated proposals reuse the
existing lifecycle without a parallel stack:
- ``source``        — "manual" | "consult" | "monthly_cycle" | "decision_funnel"
- ``shadow``        — 0/1; shadow-mode proposals are recorded, never surfaced
- ``expires_at``    — recommendation goes stale on drift (proposal expiry)
- ``funnel_run_id`` — plain audit ref to ``funnel_runs.id`` (not a DB FK, to
  keep the SQLite ADD COLUMN migration simple — mirrors ``plan_version_id``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0075_decision_funnel"
down_revision: str | None = "0074_payslip_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "funnel_runs" not in existing_tables:
        op.create_table(
            "funnel_runs",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "user_id",
                sa.String(64),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "trigger", sa.String(32), nullable=False, server_default="scheduler"
            ),
            # Shadow mode default-ON (1): the funnel records proposals but
            # surfaces NOTHING until calibrated against Ariel's real decisions.
            sa.Column("shadow", sa.Integer, nullable=False, server_default="1"),
            sa.Column(
                "status", sa.String(16), nullable=False, server_default="running"
            ),
            sa.Column("policy_version", sa.String(32), nullable=True),
            sa.Column("ips_version", sa.String(32), nullable=True),
            sa.Column("plan_version_id", sa.Integer, nullable=True),
            sa.Column("macro_read_json", sa.Text, nullable=True),
            sa.Column("totals_json", sa.Text, nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            # One run per user per calendar day (per trigger kind) — replay safe.
            sa.Column("idempotency_key", sa.Text, nullable=False),
            sa.UniqueConstraint(
                "idempotency_key", name="uq_funnel_runs_idempotency_key"
            ),
        )
        op.create_index(
            "ix_funnel_runs_user_started",
            "funnel_runs",
            ["user_id", "started_at"],
        )

    if "decision_snapshots" not in existing_tables:
        op.create_table(
            "decision_snapshots",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            # Replay-critical state is NOT NULL (codex BLOCKER): an "immutable"
            # snapshot that can't be tied back to its run / inputs / policy is
            # worthless for "why did it (not) act on X?". A snapshot is only
            # written for an actual Stage-3 decision (incl. a deliberate Hold),
            # where all of this state exists.
            sa.Column(
                "run_id",
                sa.Integer,
                sa.ForeignKey("funnel_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                sa.String(64),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("ticker", sa.String(32), nullable=False),
            # Stable idempotency key — a FULL decision-input fingerprint so a
            # legitimate same-day re-decision after ANY input change (market /
            # news / portfolio / model / prompt / policy) does NOT collide and
            # get lost (codex BLOCKER). Composition:
            #   funnel|<user>|<ticker>|<day>|<policy_version>|<model>|
            #   <prompt_hash>|<portfolio_fp>|<market_fp>
            sa.Column("dedup_key", sa.Text, nullable=False),
            # Frozen decision payload (codex BLOCKER): the snapshot answers
            # "what exactly did it decide to do and why?" INDEPENDENTLY of the
            # mutable proposals row (which can be edited / lifecycle-mutated /
            # deleted). proposal_id below is a convenience join, not the source.
            sa.Column("decision_json", sa.Text, nullable=False),
            sa.Column("model_name", sa.String(64), nullable=False),
            sa.Column("model_version", sa.String(64), nullable=True),
            sa.Column("prompt_template_hash", sa.String(64), nullable=False),
            sa.Column("temperature", sa.Float, nullable=True),
            sa.Column("seed", sa.Integer, nullable=True),
            sa.Column("model_inputs_json", sa.Text, nullable=True),
            sa.Column("source_refs_json", sa.Text, nullable=True),
            sa.Column("portfolio_snapshot_json", sa.Text, nullable=False),
            sa.Column("market_snapshot_json", sa.Text, nullable=False),
            sa.Column("policy_version", sa.String(32), nullable=False),
            sa.Column("policy_json", sa.Text, nullable=False),
            sa.Column("unchanged_explanation", sa.Text, nullable=True),
            sa.Column("why_not_act", sa.Text, nullable=True),
            sa.Column("execution_drift_json", sa.Text, nullable=True),
            sa.Column(
                "human_action_state",
                sa.String(32),
                nullable=False,
                server_default="proposed",
            ),
            sa.Column(
                "decision_run_id",
                sa.Integer,
                sa.ForeignKey("decision_runs.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "proposal_id",
                sa.Integer,
                sa.ForeignKey("proposals.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint(
                "dedup_key", name="uq_decision_snapshots_dedup_key"
            ),
        )
        op.create_index(
            "ix_decision_snapshots_run",
            "decision_snapshots",
            ["run_id"],
        )
        op.create_index(
            "ix_decision_snapshots_user_ticker",
            "decision_snapshots",
            ["user_id", "ticker"],
        )

    if "funnel_stage_rows" not in existing_tables:
        op.create_table(
            "funnel_stage_rows",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "run_id",
                sa.Integer,
                sa.ForeignKey("funnel_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            # "stage0" | "stage1" | "stage2" | "stage3" | "surface"
            sa.Column("stage", sa.String(16), nullable=False),
            # ticker, "MARKET", or "sleeve:<name>"
            sa.Column("subject", sa.String(64), nullable=False),
            # "market" | "sleeve" | "holding" | "watch"
            sa.Column("subject_type", sa.String(32), nullable=False),
            # routed | dropped | no_op | triage_go | triage_stop |
            # proposed | blocked | surfaced | hidden
            sa.Column("decision", sa.String(32), nullable=False),
            sa.Column("reason", sa.Text, nullable=False, server_default=""),
            sa.Column("signal_or_rule", sa.String(64), nullable=True),
            sa.Column("inputs_json", sa.Text, nullable=True),
            sa.Column("model", sa.String(64), nullable=True),
            sa.Column("prompt_hash", sa.String(64), nullable=True),
            sa.Column("tokens_in", sa.Integer, nullable=True),
            sa.Column("tokens_out", sa.Integer, nullable=True),
            sa.Column("cost_usd", sa.Float, nullable=True),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Column(
                "snapshot_id",
                sa.Integer,
                sa.ForeignKey("decision_snapshots.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "proposal_id",
                sa.Integer,
                sa.ForeignKey("proposals.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        op.create_index(
            "ix_funnel_stage_rows_run_stage",
            "funnel_stage_rows",
            ["run_id", "stage"],
        )

    # ---- proposals lifecycle columns (idempotent ADD COLUMN) ----
    proposal_cols = {c["name"] for c in inspector.get_columns("proposals")}
    if "source" not in proposal_cols:
        op.add_column(
            "proposals",
            sa.Column(
                "source", sa.String(32), nullable=False, server_default="manual"
            ),
        )
    if "shadow" not in proposal_cols:
        op.add_column(
            "proposals",
            sa.Column("shadow", sa.Integer, nullable=False, server_default="0"),
        )
    if "expires_at" not in proposal_cols:
        op.add_column(
            "proposals",
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "funnel_run_id" not in proposal_cols:
        op.add_column(
            "proposals",
            sa.Column("funnel_run_id", sa.Integer, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    proposal_cols = {c["name"] for c in inspector.get_columns("proposals")}
    with op.batch_alter_table("proposals") as batch:
        if "funnel_run_id" in proposal_cols:
            batch.drop_column("funnel_run_id")
        if "expires_at" in proposal_cols:
            batch.drop_column("expires_at")
        if "shadow" in proposal_cols:
            batch.drop_column("shadow")
        if "source" in proposal_cols:
            batch.drop_column("source")

    op.drop_table("funnel_stage_rows")
    op.drop_table("decision_snapshots")
    op.drop_table("funnel_runs")
