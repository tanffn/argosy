"""expense_tag_rules — durable merchant[+category] → tag brush rules.

Exact-match on merchant_normalized (optional category_slug). Applied on
ingest and retroactively when a rule is created. Substring matching is
deliberately NOT used (would catch groceries like פזית מרקט).

Revision ID: 0094_expense_tag_rules
Revises: 0093_instrument_plan_classes
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0094_expense_tag_rules"
down_revision: str | None = "0093_instrument_plan_classes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "expense_tag_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("match_merchant_normalized", sa.String(512), nullable=False),
        sa.Column("match_category_slug", sa.String(64), nullable=True),
        sa.Column("tag", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "user_id",
            "match_merchant_normalized",
            "match_category_slug",
            "tag",
            name="uq_expense_tag_rules_user_merchant_cat_tag",
        ),
    )
    op.create_index(
        "ix_expense_tag_rules_user_merchant",
        "expense_tag_rules",
        ["user_id", "match_merchant_normalized"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_expense_tag_rules_user_merchant",
        table_name="expense_tag_rules",
    )
    op.drop_table("expense_tag_rules")
