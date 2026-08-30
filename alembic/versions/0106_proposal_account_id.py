"""Route executable proposals to an exact custody account.

Revision ID: 0106_proposal_account_id
Revises: 0105_plan_repair_attempts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0106_proposal_account_id"
down_revision: str | None = "0105_plan_repair_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.add_column(
            sa.Column(
                "account_id",
                sa.String(length=64),
                nullable=False,
                server_default="",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.drop_column("account_id")
