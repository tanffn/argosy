"""plan_versions.target_allocation_overrides_json — durable authored overrides.

Revision ID: 0076_plan_allocation_overrides
Revises: 0075_decision_funnel
Create Date: 2026-07-03

Adds ``plan_versions.target_allocation_overrides_json``: a nullable JSON column
that stores ``{sleeve_label: pct}`` authored overrides on a plan version.  When
non-NULL these pin named sleeve targets through every subsequent re-synthesis so
the author's intent survives the deterministic water-fill.  NULL means "no
overrides" — identical behaviour to today.

SQLite note: ``ALTER TABLE ADD COLUMN`` is supported on every supported SQLite
(>= 3.38); no batch migration needed.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0076_plan_allocation_overrides"
down_revision: str | None = "0075_decision_funnel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_versions",
        sa.Column("target_allocation_overrides_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("plan_versions") as batch:
        batch.drop_column("target_allocation_overrides_json")
