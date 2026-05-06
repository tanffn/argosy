"""plan_versions synthesis columns (Wave 2 of plan-distillate work).

Revision ID: 0017_plan_versions_synthesis
Revises: 0016_plan_versions_distillate
Create Date: 2026-05-05

Populated only on synthesized rows (role in {draft,current,superseded}).
Baseline rows leave these NULL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_plan_versions_synthesis"
down_revision: str | Sequence[str] | None = "0016_plan_versions_distillate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("plan_versions") as batch:
        batch.add_column(sa.Column("horizon_long_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("horizon_medium_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("horizon_short_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("horizon_long_md", sa.Text(), nullable=True))
        batch.add_column(sa.Column("horizon_medium_md", sa.Text(), nullable=True))
        batch.add_column(sa.Column("horizon_short_md", sa.Text(), nullable=True))
        batch.add_column(sa.Column("synthesis_inputs_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("plan_versions") as batch:
        batch.drop_column("synthesis_inputs_json")
        batch.drop_column("horizon_short_md")
        batch.drop_column("horizon_medium_md")
        batch.drop_column("horizon_long_md")
        batch.drop_column("horizon_short_json")
        batch.drop_column("horizon_medium_json")
        batch.drop_column("horizon_long_json")
