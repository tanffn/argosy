"""decision_phases negotiation transcript table + agent_reports.phase_id (Wave C).

Revision ID: 0020_decision_phases
Revises: 0019_user_files_catalog
Create Date: 2026-05-08

Wave C of provenance: every multi-agent flow now records a row per phase
boundary. A phase aggregates the participating ``agent_reports`` rows
(linked back via the new ``agent_reports.phase_id`` FK) and stores the
parsed structured verdict from the corresponding facilitator pydantic
DTO. The TL;DR rendering and the on-disk transcript bundle live at the
``bundle_dir`` path so a user can browse the full chronological debate
on disk while the DB carries a queryable summary.

Phase ``kind`` values are namespaced by flow:
  * Trade decisions (T1-T3): ``analysts``, ``researcher_debate``,
    ``trader``, ``risk_team``, ``fund_manager``.
  * Plan synthesis (5-phase): ``plan_synth_p1`` … ``plan_synth_p5``.
  * Plan amendment chat: ``amend_apply`` (small), ``amend_synth``
    (medium), ``amend_classify`` (optional).

The ``seq`` column is a monotonically-increasing integer within one
``decision_run_id`` so the UI can render phases in chronological order
without sorting on timestamps.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_decision_phases"
down_revision: str | Sequence[str] | None = "0019_user_files_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decision_phases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "decision_run_id",
            sa.Integer(),
            sa.ForeignKey("decision_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "participants_json", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column("verdict_json", sa.Text(), nullable=True),
        sa.Column("verdict_kind", sa.String(length=64), nullable=True),
        sa.Column("tldr_md", sa.Text(), nullable=True),
        sa.Column("bundle_dir", sa.String(length=1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    op.create_index(
        "ix_decision_phases_run_seq",
        "decision_phases",
        ["decision_run_id", "seq"],
    )
    op.create_index(
        "ix_decision_phases_user_kind_started",
        "decision_phases",
        ["user_id", "kind", sa.text("started_at DESC")],
    )

    # Optional back-link from agent_reports → decision_phases. Lets the
    # Replay endpoint join phase→reports without going through verdict_json.
    with op.batch_alter_table("agent_reports") as batch:
        batch.add_column(sa.Column("phase_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_agent_reports_phase_id",
            "decision_phases",
            ["phase_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_reports") as batch:
        batch.drop_constraint("fk_agent_reports_phase_id", type_="foreignkey")
        batch.drop_column("phase_id")

    op.drop_index(
        "ix_decision_phases_user_kind_started", table_name="decision_phases"
    )
    op.drop_index("ix_decision_phases_run_seq", table_name="decision_phases")
    op.drop_table("decision_phases")
