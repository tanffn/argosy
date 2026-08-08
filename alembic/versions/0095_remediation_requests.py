"""remediation_requests + decision_overrides — data-integrity provenance.

Stream A (2026-08-07 TRLV failure class): analyst remediation requests must
be persisted rows (not prose), and open rows BLOCK green_light until
resolved or explicitly overridden with a recorded reason. Debate-loser
trade approvals and confidence-cap adjustments land in decision_overrides
so they are queryable rather than implicit in prose.

Revision ID: 0095_remediation_requests
Revises: 0094_expense_tag_rules
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0095_remediation_requests"
down_revision: str | None = "0094_expense_tag_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "remediation_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(32), nullable=True),
        sa.Column(
            "decision_run_id",
            sa.Integer(),
            sa.ForeignKey("decision_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "agent_report_id",
            sa.Integer(),
            sa.ForeignKey("agent_reports.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # price_stale | fundamentals_stale | news_empty | data_refresh |
        # data_integrity | facilitator_condition | vintage_stale
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("target_role", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        # open | resolved | overridden
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="open",
        ),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_remediation_requests_user_ticker_status",
        "remediation_requests",
        ["user_id", "ticker", "status"],
    )
    op.create_index(
        "ix_remediation_requests_decision_run",
        "remediation_requests",
        ["decision_run_id"],
    )

    op.create_table(
        "decision_overrides",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "decision_run_id",
            sa.Integer(),
            sa.ForeignKey("decision_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(32), nullable=False),
        # debate_winner_contradiction | confidence_cap
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("winning_side", sa.String(16), nullable=True),
        sa.Column("trade_action", sa.String(16), nullable=True),
        sa.Column("prior_confidence", sa.String(16), nullable=True),
        sa.Column("capped_confidence", sa.String(16), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_decision_overrides_decision_run",
        "decision_overrides",
        ["decision_run_id"],
    )
    op.create_index(
        "ix_decision_overrides_user_ticker",
        "decision_overrides",
        ["user_id", "ticker"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_decision_overrides_user_ticker", table_name="decision_overrides"
    )
    op.drop_index(
        "ix_decision_overrides_decision_run", table_name="decision_overrides"
    )
    op.drop_table("decision_overrides")
    op.drop_index(
        "ix_remediation_requests_decision_run",
        table_name="remediation_requests",
    )
    op.drop_index(
        "ix_remediation_requests_user_ticker_status",
        table_name="remediation_requests",
    )
    op.drop_table("remediation_requests")
