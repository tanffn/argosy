"""agent_reports: add cache/thinking/citations telemetry columns (Wave A).

Revision ID: 0026_agent_reports_api_telemetry
Revises: 0025_decision_phases_seq_unique
Create Date: 2026-05-22

Adds four columns to ``agent_reports`` to capture telemetry from the
Anthropic Messages API features wired into ``BaseAgent`` in Wave A:

* ``cache_input_tokens``    — tokens read from the prompt cache (priced at 0.1x
  input).
* ``cache_creation_tokens`` — tokens written to the cache (priced at 1.25x
  input, one-time per cache prefix).
* ``thinking_tokens``        — extended-thinking tokens (priced as output).
* ``citations_json``         — JSON array of cited spans from the Citations
  API, one entry per cited claim (NULL when citations disabled or unused).

Existing rows get defaults of 0 / NULL so the migration is a pure additive.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_agent_reports_api_telemetry"
down_revision: str | None = "0025_decision_phases_seq_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_reports") as batch:
        batch.add_column(sa.Column(
            "cache_input_tokens", sa.Integer(), nullable=False, server_default="0",
        ))
        batch.add_column(sa.Column(
            "cache_creation_tokens", sa.Integer(), nullable=False, server_default="0",
        ))
        batch.add_column(sa.Column(
            "thinking_tokens", sa.Integer(), nullable=False, server_default="0",
        ))
        batch.add_column(sa.Column(
            "citations_json", sa.Text(), nullable=True,
        ))


def downgrade() -> None:
    with op.batch_alter_table("agent_reports") as batch:
        batch.drop_column("citations_json")
        batch.drop_column("thinking_tokens")
        batch.drop_column("cache_creation_tokens")
        batch.drop_column("cache_input_tokens")
