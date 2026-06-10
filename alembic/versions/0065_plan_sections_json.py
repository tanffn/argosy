"""plan_sections_json — persist the structured synthesis sections.

Revision ID: 0065_plan_sections_json
Revises: 0064_proposal_plan_version_lineage
Create Date: 2026-06-10

Adds ``plan_versions.sections_json``: the flat ``PlanSynthesisOutput.sections``
list (each Section carries its own ``horizon`` + evidence contract) serialized
to JSON. The synthesizer already builds these at runtime, but they were never
persisted — so the plan-output gate reconstructed a sectionless object at
promote-time and section_coverage/evidence_per_section failed for EVERY plan.
Persisting them lets the gate evaluate the real sections. A plain nullable Text
column; NULL on existing rows (legacy → gate WARNs on the evidence checks rather
than blocking).

SQLite note: ``ALTER TABLE ADD COLUMN`` is supported on every supported SQLite
(>= 3.38); no batch migration needed.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0065_plan_sections_json"
down_revision: str | None = "0064_proposal_plan_version_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_versions",
        sa.Column("sections_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_versions", "sections_json")
