"""tax_simulation_lots — RSU/ESPP simulated tax report lots

Revision ID: 0070_tax_simulation_lots
Revises: 0069_coherence_decisions
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0070_tax_simulation_lots"
down_revision: str | None = "0069_coherence_decisions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "tax_simulation_lots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("simulation_date", sa.String(length=16), nullable=False),
        sa.Column("plan_type", sa.String(length=8), nullable=False),
        sa.Column("shares", sa.Float(), nullable=False, server_default="0"),
        sa.Column("holding_period", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("grant_id", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("grant_date", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("purchase_date", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("sale_price_usd", sa.Float(), nullable=True),
        sa.Column("cost_basis_usd", sa.Float(), nullable=True),
        sa.Column("capital_income_usd", sa.Float(), nullable=True),
        sa.Column("ordinary_income_usd", sa.Float(), nullable=True),
        sa.Column("net_proceeds_usd", sa.Float(), nullable=True),
        sa.Column("source_file_id", sa.Integer(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tax_simulation_lots_user_id", "tax_simulation_lots", ["user_id"])
    op.create_index("ix_tax_simulation_lots_simulation_date", "tax_simulation_lots", ["simulation_date"])


def downgrade() -> None:
    op.drop_index("ix_tax_simulation_lots_simulation_date", table_name="tax_simulation_lots")
    op.drop_index("ix_tax_simulation_lots_user_id", table_name="tax_simulation_lots")
    op.drop_table("tax_simulation_lots")
