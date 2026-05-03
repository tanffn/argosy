"""phase2: cadence_state, daily_briefs, prices_cache, news_cache, macro_cache.

Revision ID: 0003_phase2
Revises: 0002_phase1
Create Date: 2026-05-02

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_phase2"
down_revision: Union[str, Sequence[str], None] = "0002_phase1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cadence_state",
        sa.Column("loop_name", sa.String(length=64), primary_key=True),
        sa.Column("last_tick_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=32), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )

    op.create_table(
        "daily_briefs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("summary_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("news_report_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("macro_report_json", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "concentration_report_json", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("plan_delta_json", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_daily_briefs_user_id", "daily_briefs", ["user_id"])
    op.create_index("ix_daily_briefs_run_at", "daily_briefs", ["run_at"])

    for cache_table in ("prices_cache", "news_cache", "macro_cache"):
        op.create_table(
            cache_table,
            sa.Column("provider", sa.String(length=32), primary_key=True),
            sa.Column("key", sa.String(length=256), primary_key=True),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "retrieved_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "expires_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column("payload_hash", sa.String(length=64), nullable=False, server_default=""),
        )


def downgrade() -> None:
    for cache_table in ("macro_cache", "news_cache", "prices_cache"):
        op.drop_table(cache_table)
    op.drop_index("ix_daily_briefs_run_at", table_name="daily_briefs")
    op.drop_index("ix_daily_briefs_user_id", table_name="daily_briefs")
    op.drop_table("daily_briefs")
    op.drop_table("cadence_state")
