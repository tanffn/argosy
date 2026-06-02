"""horizon_md_audit_columns — split user-facing vs audit horizon MD.

Revision ID: 0061_horizon_md_audit_columns
Revises: 0060_objection_carry_forward
Create Date: 2026-06-02

Phase 1 of docs/plans/argosy-comprehensive-plan-integration.md.

The existing ``horizon_long_md`` / ``horizon_medium_md`` /
``horizon_short_md`` columns become the **user-facing** surface — they
get the cleaned render produced by ``_horizon_md_user``: no status
header, no ``(stated …; revisit …)`` parentheticals, no
``## Deltas vs. prior current`` block.

This migration adds three sibling ``_audit`` columns that retain the
full-fidelity render produced by ``_horizon_md_audit`` for the
``/decisions/<id>`` dev pane — status header, revisit dates, and the
deltas block are kept there for traceability.

Existing pre-0061 rows keep ``horizon_*_md`` as the original render
(which may still contain leak surfaces); the audit columns are NULL
on those rows. No backfill: the audit columns are populated forward
on every synthesis + amendment write after this migration lands.

SQLite note: ``ALTER TABLE ADD COLUMN`` is supported on every
supported SQLite (>= 3.38); no batch migration needed.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0061_horizon_md_audit_columns"
down_revision: str | None = "0060_objection_carry_forward"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_versions",
        sa.Column("horizon_long_md_audit", sa.Text(), nullable=True),
    )
    op.add_column(
        "plan_versions",
        sa.Column("horizon_medium_md_audit", sa.Text(), nullable=True),
    )
    op.add_column(
        "plan_versions",
        sa.Column("horizon_short_md_audit", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_versions", "horizon_short_md_audit")
    op.drop_column("plan_versions", "horizon_medium_md_audit")
    op.drop_column("plan_versions", "horizon_long_md_audit")
