"""plan_versions lifecycle: role + acceptance/lineage columns; decision_runs.decision_kind.

Revision ID: 0015_plan_versions_lifecycle
Revises: 0014_investor_events_dedup
Create Date: 2026-05-05

Per spec docs/superpowers/specs/2026-05-05-plan-distillate-design.md §5.1:

  - role: enum baseline | draft | current | superseded
  - accepted_at, accepted_by_user_id, superseded_at: lifecycle stamps
  - derived_from_id: lineage of synthesized rows -> baseline / prior current
  - decision_run_id: links synthesis row to fleet-review run

Plus partial unique indexes:
  - one baseline per user
  - one current per user
  - one draft per user

And on decision_runs: decision_kind column to distinguish trade-proposal
runs from plan-revision runs.

Backfill: pre-existing rows are baselines (the table previously held
imported plans only). Set role='baseline' on every pre-existing row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_plan_versions_lifecycle"
down_revision: str | Sequence[str] | None = "0014_investor_events_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add columns nullable so we can backfill, then tighten where needed.
    with op.batch_alter_table("plan_versions") as batch:
        batch.add_column(sa.Column("role", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("accepted_by_user_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("derived_from_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("decision_run_id", sa.String(length=64), nullable=True))

    # 2. Backfill role='baseline' for all existing rows.
    op.execute("UPDATE plan_versions SET role = 'baseline' WHERE role IS NULL")

    # 3. Tighten role to NOT NULL with a server default.
    with op.batch_alter_table("plan_versions") as batch:
        batch.alter_column(
            "role",
            existing_type=sa.String(length=16),
            nullable=False,
            server_default="baseline",
        )
        # FK on derived_from_id (self-referential).
        batch.create_foreign_key(
            "fk_plan_versions_derived_from",
            "plan_versions",
            ["derived_from_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 4. Partial unique indexes (SQLite-compatible WHERE clause).
    op.create_index(
        "uq_plan_versions_baseline_per_user",
        "plan_versions",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("role = 'baseline'"),
        postgresql_where=sa.text("role = 'baseline'"),
    )
    op.create_index(
        "uq_plan_versions_current_per_user",
        "plan_versions",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("role = 'current'"),
        postgresql_where=sa.text("role = 'current'"),
    )
    op.create_index(
        "uq_plan_versions_draft_per_user",
        "plan_versions",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("role = 'draft'"),
        postgresql_where=sa.text("role = 'draft'"),
    )
    # Non-unique helper for history queries.
    op.create_index(
        "ix_plan_versions_user_role",
        "plan_versions",
        ["user_id", "role"],
        unique=False,
    )

    # 5. decision_runs.decision_kind. Inspector check first — if the
    #    table does not exist (very old DBs), skip silently.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("decision_runs"):
        existing_cols = {c["name"] for c in insp.get_columns("decision_runs")}
        if "decision_kind" not in existing_cols:
            with op.batch_alter_table("decision_runs") as batch:
                batch.add_column(
                    sa.Column(
                        "decision_kind",
                        sa.String(length=32),
                        nullable=False,
                        server_default="trade_proposal",
                    )
                )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("decision_runs"):
        existing_cols = {c["name"] for c in insp.get_columns("decision_runs")}
        if "decision_kind" in existing_cols:
            with op.batch_alter_table("decision_runs") as batch:
                batch.drop_column("decision_kind")

    op.drop_index("ix_plan_versions_user_role", table_name="plan_versions")
    op.drop_index("uq_plan_versions_draft_per_user", table_name="plan_versions")
    op.drop_index("uq_plan_versions_current_per_user", table_name="plan_versions")
    op.drop_index("uq_plan_versions_baseline_per_user", table_name="plan_versions")

    with op.batch_alter_table("plan_versions") as batch:
        batch.drop_constraint("fk_plan_versions_derived_from", type_="foreignkey")
        batch.drop_column("decision_run_id")
        batch.drop_column("derived_from_id")
        batch.drop_column("superseded_at")
        batch.drop_column("accepted_by_user_id")
        batch.drop_column("accepted_at")
        batch.drop_column("role")
