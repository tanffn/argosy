"""amount_nis nullable for foreign-currency rows.

Revision ID: 0022_expense_amount_nis_nullable
Revises: 0021_household_expenses
Create Date: 2026-05-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_expense_amount_nis_nullable"
down_revision: str | None = "0021_household_expenses"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("expense_transactions") as bop:
        bop.alter_column(
            "amount_nis",
            existing_type=sa.Numeric(12, 2),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("expense_transactions") as bop:
        bop.alter_column(
            "amount_nis",
            existing_type=sa.Numeric(12, 2),
            nullable=False,
        )
