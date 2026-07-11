"""Durable per-user signal-stream success cursors.

Revision ID: 0083_signal_stream_cursors
Revises: 0082_early_signal_stream_a
Create Date: 2026-07-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0083_signal_stream_cursors"
down_revision: str | None = "0082_early_signal_stream_a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signal_stream_cursors",
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("stream", sa.String(64), primary_key=True, nullable=False),
        sa.Column(
            "last_success_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    op.drop_table("signal_stream_cursors")
