"""instrument_plan_classes — durable symbol → plan-class mapping.

Block H (2026-07-13): replaces the asset_type→US-broad catch-all with an
explicit map. Live plan instrument lists still win at resolve time; this table
stores plan-seeded rows, fleet classifications, and owner overrides (blurbs
included). Unmapped symbols fail loud — never absorbed into US-broad.

Revision ID: 0093_instrument_plan_classes
Revises: 0092_vacation_car_parent_labels
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0093_instrument_plan_classes"
down_revision: str | None = "0092_vacation_car_parent_labels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instrument_plan_classes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("plan_class_label", sa.String(128), nullable=False),
        # plan | fleet | owner — resolve precedence: live plan doc > owner >
        # fleet > plan-seeded row > Unmapped.
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("confidence", sa.String(16), nullable=False, server_default="HIGH"),
        sa.Column("what_it_is", sa.Text(), nullable=False, server_default=""),
        sa.Column("why_held", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "user_id", "symbol", name="uq_instrument_plan_classes_user_symbol"
        ),
    )
    op.create_index(
        "ix_instrument_plan_classes_user",
        "instrument_plan_classes",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_instrument_plan_classes_user", table_name="instrument_plan_classes")
    op.drop_table("instrument_plan_classes")
