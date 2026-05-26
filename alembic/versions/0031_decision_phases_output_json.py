"""decision_phases: add phase_output_json blob for resume-on-failure (T2.3).

Revision ID: 0031_decision_phases_output_json
Revises: 0030_portfolio_snapshots
Create Date: 2026-05-26

Per-phase output persistence. Previously the synthesis flow recorded ONE
``decision_phases`` row (kind='plan_synthesis') at end-of-flow only,
meaning a flake in Phase 4 forfeited the $4-6 of Opus spend on Phases
1-3. With this column the orchestrator persists each phase's output as
soon as the phase completes; a subsequent retry can load the prior
phases' outputs from DB and skip re-running them.

The column is nullable + defaults to NULL so existing rows (which were
all coarse end-of-flow records without per-phase output captured)
continue to read cleanly. New rows from the synthesis flow set it; the
resume helper reads it.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_decision_phases_output_json"
down_revision: str | None = "0030_portfolio_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("decision_phases") as batch:
        batch.add_column(sa.Column("phase_output_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("decision_phases") as batch:
        batch.drop_column("phase_output_json")
