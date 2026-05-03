"""phase3: proposals, proposals_history, approvals, decision_runs.

Revision ID: 0004_phase3
Revises: 0003_phase2
Create Date: 2026-05-02

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_phase3"
down_revision: Union[str, Sequence[str], None] = "0003_phase2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "decision_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("tier", sa.String(length=4), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column(
            "fund_manager_decision", sa.String(length=32), nullable=True
        ),
        sa.Column("proposal_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_decision_runs_user_id", "decision_runs", ["user_id"]
    )

    op.create_table(
        "proposals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column(
            "size_shares_or_currency",
            sa.Numeric(18, 4),
            nullable=False,
            server_default="0",
        ),
        sa.Column("size_units", sa.String(length=16), nullable=False, server_default="shares"),
        sa.Column("instrument", sa.String(length=16), nullable=False, server_default="stock"),
        sa.Column("order_type", sa.String(length=16), nullable=False, server_default="market"),
        sa.Column("limit_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("stop_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("time_in_force", sa.String(length=8), nullable=False, server_default="DAY"),
        sa.Column("tier", sa.String(length=4), nullable=False),
        sa.Column("account_class", sa.String(length=16), nullable=False, server_default="main"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("rationale_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("expected_impact_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("confidence", sa.String(length=16), nullable=True),
        sa.Column("cooling_off_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "decision_run_id",
            sa.Integer(),
            sa.ForeignKey("decision_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_proposals_user_id", "proposals", ["user_id"])
    op.create_index("ix_proposals_ticker", "proposals", ["ticker"])
    op.create_index("ix_proposals_tier", "proposals", ["tier"])
    op.create_index("ix_proposals_status", "proposals", ["status"])

    op.create_table(
        "proposals_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "proposal_id",
            sa.Integer(),
            sa.ForeignKey("proposals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "transitioned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "transitioned_by", sa.String(length=64), nullable=False, server_default="system"
        ),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_proposals_history_proposal_id", "proposals_history", ["proposal_id"]
    )

    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "proposal_id",
            sa.Integer(),
            sa.ForeignKey("proposals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "approval_channel", sa.String(length=32), nullable=False, server_default="dashboard"
        ),
        sa.Column(
            "second_factor_used", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("signed_token_id", sa.String(length=128), nullable=True),
    )
    op.create_index("ix_approvals_proposal_id", "approvals", ["proposal_id"])
    op.create_index("ix_approvals_user_id", "approvals", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_approvals_user_id", table_name="approvals")
    op.drop_index("ix_approvals_proposal_id", table_name="approvals")
    op.drop_table("approvals")

    op.drop_index(
        "ix_proposals_history_proposal_id", table_name="proposals_history"
    )
    op.drop_table("proposals_history")

    op.drop_index("ix_proposals_status", table_name="proposals")
    op.drop_index("ix_proposals_tier", table_name="proposals")
    op.drop_index("ix_proposals_ticker", table_name="proposals")
    op.drop_index("ix_proposals_user_id", table_name="proposals")
    op.drop_table("proposals")

    op.drop_index("ix_decision_runs_user_id", table_name="decision_runs")
    op.drop_table("decision_runs")
