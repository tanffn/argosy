"""merchant_rolling_stats: per-merchant + per-category rolling baselines.

Revision ID: 0045_merchant_rolling_stats
Revises: 0044_rsu_vest_events
Create Date: 2026-05-29

Sprint #2 (anomaly detection) commit #1 — backs Bucket A (amount outliers).
See `docs/superpowers/specs/2026-05-29-anomaly-detection-rsu-prevest-design.md`
§1.1 for the design.

The table caches robust rolling statistics for each unique
(user_id, merchant_normalized, category_id) combination over a trailing
180-day window. The nightly recompute service
`argosy/services/anomaly/rolling_stats.py::recompute_merchant_stats`
populates the table; the Bucket A detectors then read from it to compute
robust z-scores for new transactions.

Why robust stats (median + MAD) instead of mean + stdev:
real-world spend distributions are heavy-tailed (annual insurance, year-end
gifts). Raw stdev gets corrupted by exactly the outliers the detector is
trying to flag. Median + MAD are insensitive to extreme values. Mean +
stdev are retained for dashboard backward-compat (existing
`expense_dashboard.py` reads them).

`mad_nis` and `stdev_nis` are NULLABLE — when txn_count < 2 there is no
meaningful spread estimate.

UNIQUE (user_id, merchant_normalized, category_id, window_end) makes the
nightly recompute idempotent: re-running on the same window upserts in
place instead of accumulating duplicates.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_merchant_rolling_stats"
down_revision: str | None = "0044_rsu_vest_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "merchant_rolling_stats",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("merchant_normalized", sa.String(512), nullable=False),
        sa.Column(
            "category_id",
            sa.Integer,
            sa.ForeignKey("expense_categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("window_start", sa.Date, nullable=False),
        sa.Column("window_end", sa.Date, nullable=False),
        sa.Column("txn_count", sa.Integer, nullable=False),
        sa.Column("median_nis", sa.Numeric(12, 2), nullable=False),
        # NULL when txn_count < 2 (Median Absolute Deviation undefined).
        sa.Column("mad_nis", sa.Numeric(12, 2), nullable=True),
        # Kept for dashboard backward-compat.
        sa.Column("mean_nis", sa.Numeric(12, 2), nullable=False),
        sa.Column("stdev_nis", sa.Numeric(12, 2), nullable=True),
        sa.Column("min_nis", sa.Numeric(12, 2), nullable=False),
        sa.Column("max_nis", sa.Numeric(12, 2), nullable=False),
        sa.Column("first_seen_at", sa.Date, nullable=False),
        sa.Column("last_seen_at", sa.Date, nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Data-quality checks.
        sa.CheckConstraint(
            "txn_count >= 1", name="ck_merchant_rolling_stats_count_positive"
        ),
        sa.CheckConstraint(
            "window_end >= window_start",
            name="ck_merchant_rolling_stats_window_order",
        ),
        sa.CheckConstraint(
            "max_nis >= min_nis",
            name="ck_merchant_rolling_stats_max_ge_min",
        ),
        # NULL is allowed for category_id so the UniqueConstraint behavior
        # is SQLite-compatible (SQLite treats NULL as distinct in unique
        # constraints — which is what we want: rows with NULL category_id
        # can coexist for the same (user, merchant, window_end)).
        sa.UniqueConstraint(
            "user_id",
            "merchant_normalized",
            "category_id",
            "window_end",
            name="uq_merchant_rolling_stats_window",
        ),
    )
    op.create_index(
        "ix_merchant_rolling_stats_user_merchant",
        "merchant_rolling_stats",
        ["user_id", "merchant_normalized"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_merchant_rolling_stats_user_merchant",
        table_name="merchant_rolling_stats",
    )
    op.drop_table("merchant_rolling_stats")
