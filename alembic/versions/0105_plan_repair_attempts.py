"""Add immutable plan self-heal attempt receipts.

Revision ID: 0105_plan_repair_attempts
Revises: 0104_verdict_conviction_split
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0105_plan_repair_attempts"
down_revision: str | None = "0104_verdict_conviction_split"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_repair_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "plan_version_id", sa.Integer(),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "user_id", sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("base_artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("result_artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("before_violations_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("after_violations_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("affected_fields_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("instructions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("refusals_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("terminal_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_plan_repair_attempts_plan_version_id",
        "plan_repair_attempts", ["plan_version_id"],
    )
    op.create_index(
        "ix_plan_repair_attempts_user_id",
        "plan_repair_attempts", ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_plan_repair_attempts_user_id", table_name="plan_repair_attempts")
    op.drop_index("ix_plan_repair_attempts_plan_version_id", table_name="plan_repair_attempts")
    op.drop_table("plan_repair_attempts")
