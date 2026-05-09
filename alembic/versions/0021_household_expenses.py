"""household expenses subsystem (Wave EX1 — six new tables).

Revision ID: 0021_household_expenses
Revises: 0020_decision_phases
Create Date: 2026-05-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_household_expenses"
down_revision: str | None = "0020_decision_phases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "expense_sources",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),         # bank | card
        sa.Column("issuer", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("cardholder_name", sa.String(128), nullable=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "kind", "external_id",
                            name="uq_expense_sources_user_kind_external"),
    )

    op.create_table(
        "expense_statements",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source_id", sa.Integer,
                  sa.ForeignKey("expense_sources.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("file_id", sa.Integer,
                  sa.ForeignKey("user_files.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("charge_date", sa.Date, nullable=True),
        sa.Column("declared_total_nis", sa.Numeric(12, 2), nullable=True),
        sa.Column("parsed_total_nis", sa.Numeric(12, 2), nullable=False),
        sa.Column("parser_name", sa.String(32), nullable=False),
        sa.Column("parser_version", sa.String(16), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),       # parsed | failed | partial
        sa.Column("parse_error", sa.Text, nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "source_id", "period_start", "period_end",
                            name="uq_expense_statements_user_source_period"),
    )
    op.create_index("ix_expense_statements_user_period_end",
                    "expense_statements", ["user_id", "period_end"])

    op.create_table(
        "expense_categories",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=True),                                # NULL = system-default
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("label_en", sa.String(64), nullable=False),
        sa.Column("label_he", sa.String(64), nullable=False),
        sa.Column("parent_id", sa.Integer,
                  sa.ForeignKey("expense_categories.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("is_excluded_from_spend", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("is_inflow", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("display_order", sa.Integer, nullable=False,
                  server_default="0"),
        sa.UniqueConstraint("user_id", "slug", name="uq_expense_categories_user_slug"),
    )

    op.create_table(
        "expense_transactions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("statement_id", sa.Integer,
                  sa.ForeignKey("expense_statements.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source_id", sa.Integer,
                  sa.ForeignKey("expense_sources.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("occurred_on", sa.Date, nullable=False),
        sa.Column("posted_on", sa.Date, nullable=True),
        sa.Column("merchant_raw", sa.String(512), nullable=False),
        sa.Column("merchant_normalized", sa.String(512), nullable=False),
        sa.Column("amount_nis", sa.Numeric(12, 2), nullable=False),
        sa.Column("amount_orig", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency_orig", sa.String(3), nullable=True),
        sa.Column("direction", sa.String(8), nullable=False),    # debit | credit
        sa.Column("tx_type", sa.String(16), nullable=False),     # regular | standing_order | installment | refund
        sa.Column("reference", sa.String(64), nullable=True),
        sa.Column("category_id", sa.Integer,
                  sa.ForeignKey("expense_categories.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("category_source", sa.String(32), nullable=True),
        sa.Column("category_confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column("is_card_payment", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("matched_statement_id", sa.Integer,
                  sa.ForeignKey("expense_statements.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("refund_of_id", sa.Integer,
                  sa.ForeignKey("expense_transactions.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("raw_row_json", sa.Text, nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_expense_transactions_user_occurred_on",
                    "expense_transactions", ["user_id", "occurred_on"])
    op.create_index("ix_expense_transactions_user_merchant_normalized",
                    "expense_transactions", ["user_id", "merchant_normalized"])
    op.create_index("ix_expense_transactions_user_category_occurred_on",
                    "expense_transactions",
                    ["user_id", "category_id", "occurred_on"])

    op.create_table(
        "merchant_category_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("merchant_pattern", sa.String(512), nullable=False),
        sa.Column("is_regex", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("category_id", sa.Integer,
                  sa.ForeignKey("expense_categories.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source", sa.String(16), nullable=False),      # issuer_seed | llm | user
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False),
        sa.Column("hit_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "merchant_pattern", "is_regex",
                            name="uq_merchant_category_cache"),
    )
    op.create_index("ix_merchant_category_cache_user_merchant_pattern",
                    "merchant_category_cache",
                    ["user_id", "merchant_pattern"])

    op.create_table(
        "expense_review_queue",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="open"),                         # open | acknowledged | resolved | dismissed
        sa.Column("payload_json", sa.Text, nullable=False),
        sa.Column("related_tx_id", sa.Integer,
                  sa.ForeignKey("expense_transactions.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("related_source_id", sa.Integer,
                  sa.ForeignKey("expense_sources.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("user_note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_expense_review_queue_user_status_created",
                    "expense_review_queue",
                    ["user_id", "status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_expense_review_queue_user_status_created",
                  table_name="expense_review_queue")
    op.drop_table("expense_review_queue")
    op.drop_index("ix_merchant_category_cache_user_merchant_pattern",
                  table_name="merchant_category_cache")
    op.drop_table("merchant_category_cache")
    op.drop_index("ix_expense_transactions_user_category_occurred_on",
                  table_name="expense_transactions")
    op.drop_index("ix_expense_transactions_user_merchant_normalized",
                  table_name="expense_transactions")
    op.drop_index("ix_expense_transactions_user_occurred_on",
                  table_name="expense_transactions")
    op.drop_table("expense_transactions")
    op.drop_table("expense_categories")
    op.drop_index("ix_expense_statements_user_period_end",
                  table_name="expense_statements")
    op.drop_table("expense_statements")
    op.drop_table("expense_sources")
