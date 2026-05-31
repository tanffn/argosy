"""pending_reevaluations — /consult auto-retry queue.

Revision ID: 0059_pending_reevaluations
Revises: 0058_alpha_report_analyses
Create Date: 2026-05-31

When a /consult run lands at ``INSUFFICIENT_DATA`` (the trader returned
that action because load-bearing inputs were missing AFTER the
per-ticker remediation flow exhausted its retries), the system queues
a row in ``pending_reevaluations``. A daily job sweeps the queue and
re-fires the consult with the same parameters; if the retry now
completes (returns a real BUY/HOLD/SELL verdict), the user is notified
via the existing ``notification_dispatcher`` with a deep-link to the
new run.

The table is intentionally small — it's a queue, not a long-term log:

  - ``user_id, ticker, consult_mode`` uniquely identifies a pending
    consult. A second attempt at the same ticker+mode UPSERTs onto the
    existing row (increments attempt_count, refreshes last_failure_reason).
  - ``status`` is the queue position: 'pending' (will retry tomorrow),
    'resolved' (retry succeeded), 'abandoned' (exceeded max attempts).
  - ``attempt_count`` caps at a soft limit (default 7) so a persistent
    data-quality issue with the underlying source (e.g. yfinance has
    structurally bad data for this ticker) doesn't flood the retry
    queue forever. The job marks the row abandoned + dispatches a
    notification telling the user "we tried 7 times, the data didn't
    clean up — manual intervention needed (configure Finnhub key,
    check SEC EDGAR, etc.)".

Indexes
=======

  - PRIMARY KEY (id)
  - UNIQUE (user_id, ticker, consult_mode)
  - INDEX (status, last_attempted_at) for the daily sweep's
    "find pending rows older than N hours" query.

Downgrade drops the table cleanly — no data preservation since this
is an operational queue, not an audit log.

SQLite requirements: ``CURRENT_TIMESTAMP`` as a server default works
on every Argosy-supported SQLite (>= 3.38).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0059_pending_reevaluations"
down_revision: str | None = "0058_alpha_report_analyses"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VALID_STATUSES: tuple[str, ...] = ("pending", "resolved", "abandoned")
_VALID_CONSULT_MODES: tuple[str, ...] = ("tactical_trade", "long_hold")


def upgrade() -> None:
    op.create_table(
        "pending_reevaluations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("tier_value", sa.String(length=8), nullable=False),
        sa.Column("consult_mode", sa.String(length=24), nullable=False),
        sa.Column("user_constraints", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_failure_reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "last_attempted_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("resolved_decision_run_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _VALID_STATUSES)})",
            name="ck_pending_reevaluations_status",
        ),
        sa.CheckConstraint(
            f"consult_mode IN ({', '.join(repr(m) for m in _VALID_CONSULT_MODES)})",
            name="ck_pending_reevaluations_consult_mode",
        ),
        sa.CheckConstraint(
            "attempt_count >= 1",
            name="ck_pending_reevaluations_attempt_count_positive",
        ),
        sa.UniqueConstraint(
            "user_id", "ticker", "consult_mode",
            name="uq_pending_reevaluations_user_ticker_mode",
        ),
    )
    op.create_index(
        "ix_pending_reevaluations_status_last_attempted",
        "pending_reevaluations",
        ["status", "last_attempted_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pending_reevaluations_status_last_attempted",
        table_name="pending_reevaluations",
    )
    op.drop_table("pending_reevaluations")
