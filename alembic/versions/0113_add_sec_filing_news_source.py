"""Allow primary SEC earnings-filing signals in news_signals.

Revision ID: 0113_add_sec_filing_news_source
Revises: 0112_add_earnings_news_source
"""
from __future__ import annotations

from alembic import op

revision: str = "0113_add_sec_filing_news_source"
down_revision: str | None = "0112_add_earnings_news_source"
branch_labels = None
depends_on = None


def _replace_source_constraint(values: str) -> None:
    with op.batch_alter_table("news_signals") as batch:
        batch.drop_constraint("ck_news_signals_source", type_="check")
        batch.create_check_constraint(
            "ck_news_signals_source",
            f"source IN ({values})",
        )


def upgrade() -> None:
    _replace_source_constraint(
        "'discord', 'rss', 'macro_feed', 'yf_earnings', 'sec_filing'"
    )


def downgrade() -> None:
    _replace_source_constraint(
        "'discord', 'rss', 'macro_feed', 'yf_earnings'"
    )
