"""agent_reports: add run_correlation_id column (Wave B-UI follow-up).

Revision ID: 0028_agent_reports_run_correlation_id
Revises: 0027_agent_reports_sources_json
Create Date: 2026-05-23

Adds a single nullable column to ``agent_reports`` so the UI's
useDecisionStream hook can promote WS-only cascade entries to their
persisted DB row via O(1) lookup instead of the prior ±10s + agent_role
heuristic (which mis-matched multi-round same-agent runs).

Existing rows get NULL — they were persisted before BaseAgent.run() began
threading the correlation id through; the hook falls back to the legacy
heuristic for those.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_agent_reports_run_correlation_id"
down_revision: str | None = "0027_agent_reports_sources_json"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_reports") as batch:
        batch.add_column(sa.Column(
            "run_correlation_id", sa.String(length=36), nullable=True,
        ))


def downgrade() -> None:
    with op.batch_alter_table("agent_reports") as batch:
        batch.drop_column("run_correlation_id")
