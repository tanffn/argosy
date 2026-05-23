"""agent_reports: add sources_json column for prompt-source capture (Wave B-UI).

Revision ID: 0027_agent_reports_sources_json
Revises: 0026_agent_reports_api_telemetry
Create Date: 2026-05-23

Adds one nullable Text column to ``agent_reports``:

* ``sources_json`` — JSON array of ``{"source_id": str, "content": str}``
  entries representing the KB / document sources injected into the agent
  prompt via ``build_prompt``'s 3-tuple return.  NULL when the agent
  returned a 2-tuple (no sources) or sources serialization was not available.

Existing rows receive NULL (pure additive migration).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_agent_reports_sources_json"
down_revision: str | None = "0026_agent_reports_api_telemetry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_reports") as batch:
        batch.add_column(sa.Column(
            "sources_json", sa.Text(), nullable=True,
        ))


def downgrade() -> None:
    with op.batch_alter_table("agent_reports") as batch:
        batch.drop_column("sources_json")
