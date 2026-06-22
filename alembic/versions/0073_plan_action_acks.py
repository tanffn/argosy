"""Add ``plan_action_acks`` — user "mark done" for plan action-item checklist.

Backs completion of the /proposals "What's on you to do" checklist. One row per
``(user_id, item_id)`` recording that the user marked the action done while it
carried a specific ``content_fingerprint`` (a stable hash of the item's
meaningful content computed in ``_collect_action_items``).

Resurface-on-change: an item reads as acknowledged only when an ack row exists
AND its stored ``content_fingerprint`` matches the freshly recomputed one. If
the plan later edits the item, the fingerprint changes, the match fails, and the
item RESURFACES as not-done. ``(user_id, item_id)`` is UNIQUE so re-marking
upserts (replaces) rather than accumulating stale fingerprints.

Mirrors the table-creation precedent of migration 0066 (trend_scan_state):
FK to users with ON DELETE CASCADE, idempotent create, real downgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0073_plan_action_acks"
down_revision: str | None = "0072_monitor_flag_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "plan_action_acks" in inspector.get_table_names():
        return  # idempotent — already applied out-of-band

    op.create_table(
        "plan_action_acks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_id", sa.Text, nullable=False),
        sa.Column("content_fingerprint", sa.Text, nullable=False),
        sa.Column(
            "acknowledged_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "user_id", "item_id", name="uq_plan_action_acks_user_item"
        ),
    )


def downgrade() -> None:
    op.drop_table("plan_action_acks")
