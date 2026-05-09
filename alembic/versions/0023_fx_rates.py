"""fx_rates — daily exchange-rate cache (Bank of Israel).

Revision ID: 0023_fx_rates
Revises: 0022_expense_amount_nis_nullable
Create Date: 2026-05-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_fx_rates"
down_revision: str | None = "0022_expense_amount_nis_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fx_rates",
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("rate", sa.Numeric(12, 6), nullable=False),
        sa.Column("source", sa.String(length=32),
                  nullable=False, server_default="boi"),
        sa.Column("fetched_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("date", "currency", name="pk_fx_rates"),
    )
    op.create_index("idx_fx_rates_currency", "fx_rates", ["currency", "date"])


def downgrade() -> None:
    op.drop_index("idx_fx_rates_currency", table_name="fx_rates")
    op.drop_table("fx_rates")
