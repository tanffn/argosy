"""Close inbox proposals whose exact monitor source is no longer active.

Revision ID: 0110_close_inactive_flag_proposals
Revises: 0109_normalize_accepted_plan_lifecycle
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0110_close_inactive_flag_proposals"
down_revision: str | None = "0109_normalize_accepted_plan_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE action_proposals
               SET status = 'superseded',
                   decided_at = CURRENT_TIMESTAMP,
                   decided_by_user_note = 'source monitor flag is no longer active'
             WHERE status = 'open'
               AND source_flag_id IN (
                    SELECT id
                      FROM monitor_flags
                     WHERE status <> 'active'
               )
            """
        )
    )


def downgrade() -> None:
    # Reopening an item whose source is no longer authoritative is unsafe.
    pass
