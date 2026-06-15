"""real_estate_payments — durable ledger of payments toward a property purchase.

Revision ID: 0068_real_estate_payments
Revises: 0067_thesis_monitor_flag_kinds
Create Date: 2026-06-15

The portfolio snapshot (from the Family-Finances TSV) carries a per-property
Home (contract price) and Loan (remaining-to-pay) row, but the Loan is a static
figure overwritten on every re-import. This table is the canonical source for
what's been PAID: one row per payment (invoice / advance / installment), with the
remaining balance COMPUTED as ``contract price − Σ(net payments)`` so it survives
re-imports and is auditable to the source documents. ``kind='opening'`` captures
pre-ledger payments so paid-to-date is complete.

Real downgrade drops the table.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068_real_estate_payments"
down_revision: str | None = "0067_thesis_monitor_flag_kinds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "real_estate_payments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("property_key", sa.String(128), nullable=False),
        sa.Column("payment_date", sa.Date, nullable=True),
        sa.Column("invoice_no", sa.String(64), nullable=True),
        sa.Column(
            "amount_net_local", sa.Float, nullable=False, server_default=sa.text("0")
        ),
        sa.Column("vat_local", sa.Float, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "currency", sa.String(8), nullable=False, server_default=sa.text("'EUR'")
        ),
        sa.Column(
            "kind",
            sa.String(24),
            nullable=False,
            server_default=sa.text("'installment'"),
        ),
        sa.Column("description", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("source_file_id", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "kind IN ('opening', 'advance', 'installment', 'handover', 'other')",
            name="ck_real_estate_payments_kind",
        ),
    )
    op.create_index(
        "ix_real_estate_payments_user_property",
        "real_estate_payments",
        ["user_id", "property_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_real_estate_payments_user_property", table_name="real_estate_payments"
    )
    op.drop_table("real_estate_payments")
