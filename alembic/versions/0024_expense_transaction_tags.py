"""expense_transactions.tags — JSON-encoded tag list (Wave EX4 Q1B).

Revision ID: 0024_expense_transaction_tags
Revises: 0023_fx_rates
Create Date: 2026-05-10

Adds a TEXT column ``tags`` to ``expense_transactions``. Stores a JSON
array of tag strings (e.g. ``["trip:greece-2026-aug"]``). Tags overlay
on top of category — a row can have category=``dining_out.restaurants``
AND tag ``trip:greece-2026-aug`` simultaneously. SQLite has no functional
index on JSON arrays; at single-user scale a ``LIKE '%"<tag>"%'`` scan
behind the user_id WHERE clause is fine.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_expense_transaction_tags"
down_revision: str | None = "0023_fx_rates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("expense_transactions") as batch:
        batch.add_column(sa.Column(
            "tags", sa.Text(), nullable=False, server_default="[]",
        ))


def downgrade() -> None:
    with op.batch_alter_table("expense_transactions") as batch:
        batch.drop_column("tags")
