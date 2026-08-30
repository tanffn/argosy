"""Add durable per-ticker earnings calendar coverage receipts.

Revision ID: 0111_earnings_coverage_receipts
Revises: 0110_close_inactive_flag_proposals
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0111_earnings_coverage_receipts"
down_revision: str | None = "0110_close_inactive_flag_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "earnings_coverage_receipts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("check_date", sa.Date(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("events_json", sa.Text(), nullable=False),
        sa.Column("latest_reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'empty', 'error')",
            name="ck_earnings_coverage_receipts_status",
        ),
        sa.CheckConstraint(
            "json_valid(events_json)",
            name="ck_earnings_coverage_receipts_events_json",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "ticker",
            "provider",
            "check_date",
            name="uq_earnings_coverage_daily_check",
        ),
    )
    op.create_index(
        "ix_earnings_coverage_user_checked",
        "earnings_coverage_receipts",
        ["user_id", "checked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_earnings_coverage_user_checked",
        table_name="earnings_coverage_receipts",
    )
    op.drop_table("earnings_coverage_receipts")
