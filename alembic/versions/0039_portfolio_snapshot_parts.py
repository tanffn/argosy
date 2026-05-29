"""portfolio_snapshot_parts: pending XLS upload awaiting paired Osh statement.

Revision ID: 0039_portfolio_snapshot_parts
Revises: 0038_anomaly_reports
Create Date: 2026-05-29

Adds the ``portfolio_snapshot_parts`` table. One row per Leumi portfolio XLS
upload that arrived without a same-month Osh (current-account) statement
already in the DB. The Osh-side hook (``try_resolve_pending_on_osh_arrival``)
walks this table when a new Osh statement is committed and resolves the
pair via TSV synthesis.

Why a separate table from ``portfolio_snapshots``:
  * ``portfolio_snapshots`` is the parsed-TSV cache — a *complete* snapshot.
  * ``portfolio_snapshot_parts`` is a transient queue — *incomplete* state
    waiting for the other half. Pollution risk if mashed together.

SHA-256 of XLS contents is the idempotency key — re-upload of the same file
returns the same row instead of duplicating work.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_portfolio_snapshot_parts"
down_revision: str | None = "0038_anomaly_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshot_parts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("portfolio_number", sa.String(length=64), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "paired_osh_statement_id", sa.Integer(), nullable=True,
        ),
        sa.Column("paired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_tsv_path", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["paired_osh_statement_id"],
            ["expense_statements.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "user_id", "sha256",
            name="uq_portfolio_snapshot_parts_user_sha",
        ),
        # Semantic idempotency: re-export of the same snapshot
        # (different bytes, same date + Leumi portfolio number) dedups
        # to the same row. Codex zigzag #9 (2026-05-29) flagged that
        # SHA-of-bytes alone misses incidental XML reordering.
        # portfolio_number nullable -> some XLS exports omit the
        # cell; those rows fall back to sha-only dedup.
        sa.UniqueConstraint(
            "user_id", "snapshot_date", "portfolio_number",
            name="uq_portfolio_snapshot_parts_user_date_portfolio",
        ),
    )
    op.create_index(
        "ix_portfolio_snapshot_parts_user_status_date",
        "portfolio_snapshot_parts",
        ["user_id", "status", "snapshot_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_snapshot_parts_user_status_date",
        table_name="portfolio_snapshot_parts",
    )
    op.drop_table("portfolio_snapshot_parts")
