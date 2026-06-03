"""plan_narrative_persistence — persist the bilingual plan narrative.

Revision ID: 0062_plan_narrative_persistence
Revises: 0061_horizon_md_audit_columns
Create Date: 2026-06-03

The plan narrative (the /plan bilingual recap) was cached only in the
uvicorn process's memory, so every backend restart wiped it and the next
/plan load paid a multi-minute LLM regen ("Generating narrative — first
request takes a few seconds…", which was actually minutes).

This adds a ``plan_versions.narrative_json`` column. The narrative
service write-through-persists the generated narrative here keyed by the
plan_version row, then reads it back on load — surviving restarts and
returning instantly. The in-memory cache stays as the hot layer.

Stores a JSON blob: {narrative_md_en, narrative_md_he, confidence}.
NULL on existing rows; populated forward on the next generation.

SQLite note: ``ALTER TABLE ADD COLUMN`` is supported on every supported
SQLite (>= 3.38); no batch migration needed.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0062_plan_narrative_persistence"
down_revision: str | None = "0061_horizon_md_audit_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_versions",
        sa.Column("narrative_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_versions", "narrative_json")
