"""holding_reviews — one queryable audit row per holdings-review verdict.

Tracking-state audit (2026-07-08) FIX 3: the daily ``holdings_review`` job
decided BUY/HOLD/SELL/TRIM per material holding but persisted only the
actionable, blind-verified survivors (open ActionProposals). HOLD verdicts,
dedup-skipped writes and ``held_unverified`` verdicts (actionable but failed
the blind re-derivation) vanished into logs — the review was structurally
un-auditable. "Nothing hidden — reviewed means a queryable row."

A dedicated small table (NOT resolved ActionProposal rows, which would pollute
the inbox surfaces): user_id, symbol, reviewed_at, verdict, confidence, reason,
evidence_json, position_usd, elevated_by_flag, outcome
(proposed | held_unverified | hold | dedup_skipped).

Revision ID: 0079_holding_reviews
Revises: 0078_action_proposal_status_executed
Create Date: 2026-07-08
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0079_holding_reviews"
down_revision: str | None = "0078_action_proposal_status_executed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "holding_reviews",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("confidence", sa.String(16), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("position_usd", sa.Float(), nullable=True),
        sa.Column(
            "elevated_by_flag",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("outcome", sa.String(32), nullable=False),
    )
    op.create_index(
        "ix_holding_reviews_user_symbol",
        "holding_reviews",
        ["user_id", "symbol", "reviewed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_holding_reviews_user_symbol", table_name="holding_reviews")
    op.drop_table("holding_reviews")
