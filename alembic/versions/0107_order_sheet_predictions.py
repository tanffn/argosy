"""Score executable order expectations on their authored due dates.

Revision ID: 0107_order_sheet_predictions
Revises: 0106_proposal_account_id
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0107_order_sheet_predictions"
down_revision: str | None = "0106_proposal_account_id"
branch_labels = None
depends_on = None


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
        [
            {
                "method_name": "order_sheet_due_date_v1",
                "family": "fixed_lookahead",
                "method_version": 1,
                "description": (
                    "Score an executable order at its authored expectation due date; "
                    "the writer persists that exact due date instead of the generic 30d cap."
                ),
                "is_active": 1,
            }
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM prediction_outcomes "
            "WHERE evaluation_method = 'order_sheet_due_date_v1'"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM predictions "
            "WHERE evaluation_method = 'order_sheet_due_date_v1'"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM evaluation_method_registry "
            "WHERE method_name = 'order_sheet_due_date_v1'"
        )
    )
