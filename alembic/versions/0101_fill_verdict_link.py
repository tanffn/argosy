"""fill→verdict link — additive nullable fills.verdict_id (seam 4).

Closes the "did the user act on verdict V?" gap: a fill previously linked only
to a PROPOSAL (fills.proposal_id → proposals.id). This adds a nullable
``verdict_id`` so an executed fill can point directly at the settled verdict
that recommended it, resolved best-effort at reconcile time via
``fills.proposal_id → proposals.decision_run_id ↔ verdicts.source_decision_run_id``.

Additive + nullable + reversible. Plain nullable ref (not a DB-enforced FK) to
keep the SQLite ADD COLUMN migration simple, mirroring proposals.plan_version_id.
Safe today: 0 fills exist. NULL for any existing row; existing rows/behavior
are otherwise untouched.

This migration does NOT touch the live db/argosy.db.

Revision ID: 0101_fill_verdict_link
Revises: 0100_observed_decision_run_id
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0101_fill_verdict_link"
down_revision: str | None = "0100_observed_decision_run_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fills",
        sa.Column("verdict_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_fills_verdict_id", "fills", ["verdict_id"])


def downgrade() -> None:
    op.drop_index("ix_fills_verdict_id", table_name="fills")
    op.drop_column("fills", "verdict_id")
