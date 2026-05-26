"""daily_briefs: add brief_date + content_md + decision_run_id (T4.5).

Revision ID: 0034_daily_briefs_runner
Revises: 0033_decision_kind_expansion
Create Date: 2026-05-26

T4.5 introduces a new lightweight daily-brief runner that produces a
single one-pager markdown blob (``content_md``) keyed by a per-user
``brief_date`` for idempotent re-runs, with a back-pointer to the
``decision_runs`` row that produced it (``decision_run_id``).

We keep the legacy columns from Phase 2 (``summary_text`` +
``news_report_json`` + ``macro_report_json`` + ``concentration_report_json``
+ ``plan_delta_json``) so the existing ``DailyBriefLoop`` and the
``/api/daily-brief/latest`` route keep working. The new runner writes
only ``content_md`` (the legacy four-report fields default to empty
strings) plus the new metadata columns.

Idempotency: a UNIQUE index on ``(user_id, brief_date)`` ensures
re-running the runner for the same calendar day updates the existing
row rather than creating a duplicate.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_daily_briefs_runner"
down_revision: str | None = "0033_decision_kind_expansion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("daily_briefs") as batch:
        # Calendar date the brief covers. Nullable for back-compat with
        # legacy rows that were keyed only by ``run_at``; new rows always
        # set this so the unique index applies.
        batch.add_column(sa.Column("brief_date", sa.Date(), nullable=True))
        # The one-pager markdown produced by the T4.5 runner. Empty
        # string on legacy rows.
        batch.add_column(
            sa.Column(
                "content_md", sa.Text(), nullable=False, server_default=""
            )
        )
        # Back-pointer to decision_runs (decision_kind='daily_brief').
        # Nullable because legacy rows don't have one.
        batch.add_column(
            sa.Column("decision_run_id", sa.Integer(), nullable=True)
        )
    # Partial unique index so the one-row-per-(user_id, brief_date)
    # invariant holds for new T4.5 rows without affecting historical
    # NULL-brief_date legacy rows.
    op.create_index(
        "uq_daily_briefs_user_date",
        "daily_briefs",
        ["user_id", "brief_date"],
        unique=True,
        sqlite_where=sa.text("brief_date IS NOT NULL"),
        postgresql_where=sa.text("brief_date IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_daily_briefs_user_date", table_name="daily_briefs")
    with op.batch_alter_table("daily_briefs") as batch:
        batch.drop_column("decision_run_id")
        batch.drop_column("content_md")
        batch.drop_column("brief_date")
