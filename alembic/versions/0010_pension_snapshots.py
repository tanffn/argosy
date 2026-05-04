"""phase3 (Israeli pension): pension_fund_snapshots

Revision ID: 0010_pension_snapshots
Revises: 0009_drop_orphan_user_context_id
Create Date: 2026-05-02

Adds the `pension_fund_snapshots` table — per-user, per-fund time-series
of gemelnet (MoF) performance data. Populated by `argosy gemelnet
refresh-user`. Each row captures one moment-in-time snapshot of a fund's
12-month return, the benchmark, the relative gap, and (optionally) the
user's reported NIS balance. `source_url` is the canonical MoF page so
downstream agents can cite a primary source.

The compound index `(user_id, fund_id, snapshot_at)` makes the most
common access pattern (latest snapshot per fund per user) an index seek.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_pension_snapshots"
down_revision: Union[str, Sequence[str], None] = "0009_drop_orphan_user_context_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pension_fund_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fund_id", sa.String(length=64), nullable=False),
        sa.Column("fund_name", sa.String(length=256), nullable=True),
        sa.Column("fund_type", sa.String(length=32), nullable=True),
        sa.Column("manager", sa.String(length=128), nullable=True),
        sa.Column("return_pct_12m", sa.Numeric(8, 4), nullable=True),
        sa.Column("benchmark_return_pct_12m", sa.Numeric(8, 4), nullable=True),
        sa.Column("relative_to_benchmark_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("balance_nis", sa.Numeric(18, 2), nullable=True),
        sa.Column(
            "snapshot_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("source_url", sa.String(length=512), nullable=True),
    )
    op.create_index(
        "ix_pension_fund_snapshots_user_fund_time",
        "pension_fund_snapshots",
        ["user_id", "fund_id", "snapshot_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pension_fund_snapshots_user_fund_time",
        table_name="pension_fund_snapshots",
    )
    op.drop_table("pension_fund_snapshots")
