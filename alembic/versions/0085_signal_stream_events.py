"""Tenant-scoped raw event ledger for signal streams.

Revision ID: 0085_signal_stream_events
Revises: 0084_signal_stream_warning_kind
Create Date: 2026-07-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0085_signal_stream_events"
down_revision: str | None = "0084_signal_stream_warning_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signal_stream_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stream", sa.String(64), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("event_group_key", sa.Text(), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=False),
        sa.Column("event_at", sa.Date(), nullable=False),
        sa.Column("available_at", sa.Date(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("source_urls_json", sa.Text(), nullable=False),
        sa.Column(
            "active",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "evaluation_pending",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("3"),
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "json_valid(payload_json)",
            name="ck_signal_stream_events_payload_json",
        ),
        sa.CheckConstraint(
            "json_valid(source_urls_json)",
            name="ck_signal_stream_events_source_urls_json",
        ),
        sa.CheckConstraint(
            "active IN (0, 1)",
            name="ck_signal_stream_events_active",
        ),
        sa.CheckConstraint(
            "evaluation_pending IN (0, 1, 2, 3)",
            name="ck_signal_stream_events_evaluation_pending",
        ),
        sa.UniqueConstraint(
            "user_id",
            "stream",
            "event_key",
            name="uq_signal_stream_events_user_stream_event",
        ),
    )
    op.create_index(
        "ix_signal_events_user_stream_available",
        "signal_stream_events",
        ["user_id", "stream", "available_at"],
    )
    op.create_index(
        "ix_signal_events_user_stream_ticker_event",
        "signal_stream_events",
        ["user_id", "stream", "ticker", "event_at"],
    )
    op.create_index(
        "ix_signal_events_user_stream_group",
        "signal_stream_events",
        ["user_id", "stream", "event_group_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_signal_events_user_stream_group",
        table_name="signal_stream_events",
    )
    op.drop_index(
        "ix_signal_events_user_stream_ticker_event",
        table_name="signal_stream_events",
    )
    op.drop_index(
        "ix_signal_events_user_stream_available",
        table_name="signal_stream_events",
    )
    op.drop_table("signal_stream_events")
