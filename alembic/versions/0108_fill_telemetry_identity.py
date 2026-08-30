"""Make live fill reconciliation account-scoped and idempotent.

Revision ID: 0108_fill_telemetry_identity
Revises: 0107_order_sheet_predictions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0108_fill_telemetry_identity"
down_revision: str | None = "0107_order_sheet_predictions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pending_orders") as batch:
        batch.add_column(
            sa.Column("account_id", sa.String(64), nullable=False, server_default="")
        )
        batch.create_index("ix_pending_orders_account_id", ["account_id"])
    with op.batch_alter_table("fills") as batch:
        batch.add_column(
            sa.Column("external_fill_id", sa.String(128), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("account_id", sa.String(64), nullable=False, server_default="")
        )
        batch.create_index("ix_fills_external_fill_id", ["external_fill_id"])
        batch.create_index("ix_fills_account_id", ["account_id"])
    op.create_index(
        "uq_fills_broker_execution",
        "fills",
        ["user_id", "broker", "external_fill_id"],
        unique=True,
        sqlite_where=sa.text("external_fill_id <> ''"),
    )


def downgrade() -> None:
    op.drop_index("uq_fills_broker_execution", table_name="fills")
    with op.batch_alter_table("fills") as batch:
        batch.drop_index("ix_fills_account_id")
        batch.drop_index("ix_fills_external_fill_id")
        batch.drop_column("account_id")
        batch.drop_column("external_fill_id")
    with op.batch_alter_table("pending_orders") as batch:
        batch.drop_index("ix_pending_orders_account_id")
        batch.drop_column("account_id")
