"""hardening: intake session_id tracking

Revision ID: 0008_intake_session
Revises: 0007_phase6
Create Date: 2026-05-03

Adds an explicit intake_session_id UUID that groups all turns of one
intake conversation. Generated on stage_1 entry; persisted on
user_context; stamped onto every agent_reports row produced during
that session. Lets the audit log answer "show me every Claude call
made during Ariel's third intake attempt" with a single WHERE clause.

Both columns are nullable + indexed; backwards compatible with
existing rows (which simply have NULL session_id).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_intake_session"
down_revision: Union[str, Sequence[str], None] = "0007_phase6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("user_context") as batch:
        batch.add_column(
            sa.Column("intake_session_id", sa.String(length=64), nullable=True)
        )
        batch.create_index(
            "ix_user_context_intake_session_id",
            ["intake_session_id"],
        )

    with op.batch_alter_table("agent_reports") as batch:
        batch.add_column(
            sa.Column("intake_session_id", sa.String(length=64), nullable=True)
        )
        batch.create_index(
            "ix_agent_reports_intake_session_id",
            ["intake_session_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_reports") as batch:
        batch.drop_index("ix_agent_reports_intake_session_id")
        batch.drop_column("intake_session_id")
    with op.batch_alter_table("user_context") as batch:
        batch.drop_index("ix_user_context_intake_session_id")
        batch.drop_column("intake_session_id")
