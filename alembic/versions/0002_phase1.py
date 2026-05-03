"""phase1: plan_versions, plan_critiques, agent_reports, agent_reports_blobs;
add user_context.current_stage.

Revision ID: 0002_phase1
Revises: 0001_initial
Create Date: 2026-05-02

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_phase1"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # user_context: add current_stage TEXT NULLABLE
    with op.batch_alter_table("user_context") as batch:
        batch.add_column(sa.Column("current_stage", sa.String(length=32), nullable=True))

    op.create_table(
        "plan_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_label", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("source_path", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("raw_markdown", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_plan_versions_user_id", "plan_versions", ["user_id"])

    op.create_table(
        "plan_critiques",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_version_id",
            sa.Integer(),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("critique_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_plan_critiques_user_id", "plan_critiques", ["user_id"])
    op.create_index("ix_plan_critiques_plan_version_id", "plan_critiques", ["plan_version_id"])

    op.create_table(
        "agent_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_role", sa.String(length=64), nullable=False),
        sa.Column("decision_id", sa.String(length=64), nullable=True),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("response_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("model", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("confidence", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_agent_reports_user_id", "agent_reports", ["user_id"])
    op.create_index("ix_agent_reports_agent_role", "agent_reports", ["agent_role"])
    op.create_index("ix_agent_reports_decision_id", "agent_reports", ["decision_id"])

    op.create_table(
        "agent_reports_blobs",
        sa.Column(
            "report_id",
            sa.Integer(),
            sa.ForeignKey("agent_reports.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_table("agent_reports_blobs")
    op.drop_index("ix_agent_reports_decision_id", table_name="agent_reports")
    op.drop_index("ix_agent_reports_agent_role", table_name="agent_reports")
    op.drop_index("ix_agent_reports_user_id", table_name="agent_reports")
    op.drop_table("agent_reports")
    op.drop_index("ix_plan_critiques_plan_version_id", table_name="plan_critiques")
    op.drop_index("ix_plan_critiques_user_id", table_name="plan_critiques")
    op.drop_table("plan_critiques")
    op.drop_index("ix_plan_versions_user_id", table_name="plan_versions")
    op.drop_table("plan_versions")
    with op.batch_alter_table("user_context") as batch:
        batch.drop_column("current_stage")
