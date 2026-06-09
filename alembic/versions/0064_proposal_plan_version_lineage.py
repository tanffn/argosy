"""proposal_plan_version_lineage — trace each proposal to its canonical plan.

Revision ID: 0064_proposal_plan_version_lineage
Revises: 0063_plan_target_allocation
Create Date: 2026-06-09

Adds ``proposals.plan_version_id``: the canonical ``PlanVersion`` a proposal was
generated against (audit lineage, roadmap T4.4). A plain nullable Integer (a
logical reference to ``plan_versions.id``, not a DB-enforced FK) so the SQLite
``ALTER TABLE ADD COLUMN`` stays simple and matches the ORM model. NULL on
existing rows; stamped best-effort at proposal-persist time going forward.

SQLite note: ``ALTER TABLE ADD COLUMN`` is supported on every supported SQLite
(>= 3.38); no batch migration needed.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0064_proposal_plan_version_lineage"
down_revision: str | None = "0063_plan_target_allocation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "proposals",
        sa.Column("plan_version_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("proposals", "plan_version_id")
