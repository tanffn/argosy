"""agent_reports: add system_prompt + user_prompt columns (Wave B-UI follow-up #2).

Revision ID: 0029_agent_reports_prompts
Revises: 0028_agent_reports_run_correlation_id
Create Date: 2026-05-23

Persists the full system + user prompts threaded through BaseAgent.run()
so the UI's AgentDetailDrawer can show what the agent was actually sent,
not just the SHA256 prompt_hash. Existing rows get NULL; the new /prompt
endpoint surfaces this as a "Prompt not captured" empty state.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_agent_reports_prompts"
down_revision: str | None = "0028_agent_reports_run_correlation_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_reports") as batch:
        batch.add_column(sa.Column("system_prompt", sa.Text(), nullable=True))
        batch.add_column(sa.Column("user_prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_reports") as batch:
        batch.drop_column("user_prompt")
        batch.drop_column("system_prompt")
