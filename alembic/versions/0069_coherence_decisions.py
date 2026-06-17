"""coherence_decisions ledger

Revision ID: 0069_coherence_decisions
Revises: 0068_real_estate_payments
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0069_coherence_decisions"
down_revision: str | None = "0068_real_estate_payments"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "coherence_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("decision_run_id", sa.Integer(), nullable=True),
        sa.Column("dispute_key", sa.String(length=32), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False, server_default=""),
        sa.Column("ruling", sa.Text(), nullable=False, server_default=""),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("basis", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("resolved_by", sa.String(length=16), nullable=False),
        sa.Column("coherence_invariant_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("conformed_surfaces_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("superseded_by_id", sa.Integer(), sa.ForeignKey("coherence_decisions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_coherence_decisions_user_id", "coherence_decisions", ["user_id"])
    op.create_index("ix_coherence_decisions_decision_run_id", "coherence_decisions", ["decision_run_id"])
    op.create_index("ix_coherence_decisions_dispute_key", "coherence_decisions", ["dispute_key"])


def downgrade() -> None:
    op.drop_index("ix_coherence_decisions_dispute_key", table_name="coherence_decisions")
    op.drop_index("ix_coherence_decisions_decision_run_id", table_name="coherence_decisions")
    op.drop_index("ix_coherence_decisions_user_id", table_name="coherence_decisions")
    op.drop_table("coherence_decisions")
