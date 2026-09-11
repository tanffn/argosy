"""Add one-year recommendation outcome methods.

Revision ID: 0114_recommendation_self_evaluation
Revises: 0113_add_sec_filing_news_source
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0114_recommendation_self_evaluation"
down_revision: str | None = "0113_add_sec_filing_news_source"
branch_labels = None
depends_on = None


_METHODS = (
    {
        "method_name": "fixed_lookahead_365d",
        "family": "fixed_lookahead",
        "method_version": 1,
        "description": (
            "One-year fixed-lookahead score for a dated recommendation; "
            "the evaluation window is 365 calendar days."
        ),
        "is_active": 1,
    },
    {
        "method_name": "fixed_lookahead_365d_entry_backfilled",
        "family": "fixed_lookahead",
        "method_version": 2,
        "description": (
            "Entry-backfilled v2 of fixed_lookahead_365d; preserves the "
            "original recommendation timestamp when its quote was missing."
        ),
        "is_active": 1,
    },
)


def upgrade() -> None:
    op.bulk_insert(
        sa.table(
            "evaluation_method_registry",
            sa.column("method_name", sa.Text),
            sa.column("family", sa.Text),
            sa.column("method_version", sa.Integer),
            sa.column("description", sa.Text),
            sa.column("is_active", sa.Integer),
        ),
        list(_METHODS),
    )


def downgrade() -> None:
    names = [row["method_name"] for row in _METHODS]
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM prediction_outcomes WHERE evaluation_method IN :names"
        ).bindparams(sa.bindparam("names", expanding=True)),
        {"names": names},
    )
    bind.execute(
        sa.text(
            "DELETE FROM predictions WHERE evaluation_method IN :names"
        ).bindparams(sa.bindparam("names", expanding=True)),
        {"names": names},
    )
    bind.execute(
        sa.text(
            "DELETE FROM evaluation_method_registry WHERE method_name IN :names"
        ).bindparams(sa.bindparam("names", expanding=True)),
        {"names": names},
    )
