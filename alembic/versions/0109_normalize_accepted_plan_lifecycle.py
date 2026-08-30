"""Normalize lifecycle metadata on already-accepted current plans.

Revision ID: 0109_normalize_accepted_plan_lifecycle
Revises: 0108_fill_telemetry_identity

Older promotion code left ``-fm-rejected`` on a plan even after the user
explicitly accepted it.  The state observer then treated historical review
metadata as a current blocking authority.  Promotion now normalizes the label;
this data migration repairs existing accepted plans and closes only alerts
whose source is that stale lifecycle suffix.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0109_normalize_accepted_plan_lifecycle"
down_revision: str | None = "0108_fill_telemetry_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE plan_versions
               SET version_label = substr(version_label, 1, length(version_label) - 12)
             WHERE role = 'current'
               AND accepted_at IS NOT NULL
               AND version_label LIKE '%-fm-rejected'
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE monitor_flags
               SET status = 'superseded'
             WHERE kind = 'state_observer_plan_assumption_observation'
               AND status = 'active'
               AND acknowledged_at IS NULL
               AND payload LIKE '%-fm-rejected%'
               AND EXISTS (
                    SELECT 1
                      FROM plan_versions pv
                     WHERE pv.user_id = monitor_flags.user_id
                       AND pv.role = 'current'
                       AND pv.accepted_at IS NOT NULL
               )
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE action_proposals
               SET status = 'superseded',
                   decided_at = CURRENT_TIMESTAMP,
                   decided_by_user_note =
                       'stale rejected-plan lifecycle alert closed after accepted-plan normalization'
             WHERE status = 'open'
               AND source_flag_id IN (
                    SELECT id
                      FROM monitor_flags
                     WHERE kind = 'state_observer_plan_assumption_observation'
                       AND status = 'superseded'
                       AND payload LIKE '%-fm-rejected%'
               )
            """
        )
    )


def downgrade() -> None:
    # Accepted-plan lifecycle normalization is intentionally irreversible:
    # restoring a rejected suffix would recreate contradictory authority.
    pass
