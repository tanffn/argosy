"""plan_target_allocation — persist the canonical TargetAllocationDoc.

Revision ID: 0063_plan_target_allocation
Revises: 0062_plan_narrative_persistence
Create Date: 2026-06-09

Adds ``plan_versions.target_allocation_json``: the structured, instrument-level,
time-varying target allocation (the deterministic ``allocation_plan`` engine's
output) that every surface — the /plan glidepath, the /portfolio target pie, the
/retirement glide — projects rather than re-deriving. NULL on existing rows;
populated forward on synthesis and backfilled for the current plan
(roadmap T1.4-T1.6).

SQLite note: ``ALTER TABLE ADD COLUMN`` is supported on every supported SQLite
(>= 3.38); no batch migration needed.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0063_plan_target_allocation"
down_revision: str | None = "0062_plan_narrative_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_versions",
        sa.Column("target_allocation_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_versions", "target_allocation_json")
