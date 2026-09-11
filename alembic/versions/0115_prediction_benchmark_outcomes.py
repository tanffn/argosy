"""Add immutable S&P 500-relative prediction outcomes.

Revision ID: 0115_prediction_benchmark_outcomes
Revises: 0114_recommendation_self_evaluation
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0115_prediction_benchmark_outcomes"
down_revision: str | None = "0114_recommendation_self_evaluation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prediction_benchmark_outcomes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "prediction_outcome_id",
            sa.Integer,
            sa.ForeignKey(
                "prediction_outcomes.id",
                ondelete="CASCADE",
                name="fk_prediction_benchmark_outcomes_outcome_id",
            ),
            nullable=False,
        ),
        sa.Column("benchmark_symbol", sa.Text, nullable=False),
        sa.Column("benchmark_version", sa.Text, nullable=False),
        sa.Column("comparison_mode", sa.Text, nullable=False),
        sa.Column("benchmark_start_date", sa.Date, nullable=False),
        sa.Column("benchmark_end_date", sa.Date, nullable=False),
        sa.Column("benchmark_entry_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("benchmark_exit_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("benchmark_return_pct", sa.Numeric(11, 6), nullable=False),
        sa.Column("subject_return_pct", sa.Numeric(11, 6), nullable=False),
        sa.Column(
            "decision_excess_return_pct", sa.Numeric(11, 6), nullable=False
        ),
        sa.Column("evidence_json", sa.Text, nullable=False),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "comparison_mode IN ('own_vs_benchmark', 'avoid_vs_benchmark')",
            name="ck_prediction_benchmark_outcomes_mode",
        ),
        sa.CheckConstraint(
            "json_valid(evidence_json)",
            name="ck_prediction_benchmark_outcomes_evidence_json",
        ),
        sa.UniqueConstraint(
            "prediction_outcome_id",
            "benchmark_symbol",
            "benchmark_version",
            name="uq_prediction_benchmark_outcome_version",
        ),
    )
    op.create_index(
        "ix_prediction_benchmark_outcomes_evaluated",
        "prediction_benchmark_outcomes",
        ["evaluated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_prediction_benchmark_outcomes_evaluated",
        table_name="prediction_benchmark_outcomes",
    )
    op.drop_table("prediction_benchmark_outcomes")
