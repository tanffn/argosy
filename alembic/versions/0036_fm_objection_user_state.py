"""fm_objection_user_state: per-(plan_version, objection) user stance.

Revision ID: 0036_fm_objection_user_state
Revises: 0034_daily_briefs_runner
Create Date: 2026-05-26

Adds the ``fm_objection_user_state`` table so the user can express a
per-FM-objection stance (AGREE / DISAGREE / DEFER) plus an optional
free-text counter-position when disagreeing.  State is keyed by
``(user_id, plan_version_id, objection_index)`` so multiple drafts can
each carry independent decisions and the user's choices survive a page
navigation away from /plan and back.

``topic_hash`` is defense-in-depth — if the FM ever re-orders / mutates
its objection list across re-renders (the list is parsed live from the
``fund_manager`` agent_report on every GET), the hash gives the API a
way to detect a stale row and either drop or surface it as deferred.

The companion endpoint ``POST /api/plan/draft/objections/start-new-round``
reads this table, composes a structured guidance string, and routes
through the existing advisor check-in flow so the cost-cap wiring is
reused unchanged.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_fm_objection_user_state"
down_revision: str | None = "0035_fm_objection_translations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fm_objection_user_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "plan_version_id",
            sa.Integer(),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("objection_index", sa.Integer(), nullable=False),
        sa.Column("topic_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "stance",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column("counter_position", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "stance IN ('AGREE','DISAGREE','DEFER')",
            name="ck_fm_obj_state_stance",
        ),
        sa.UniqueConstraint(
            "user_id",
            "plan_version_id",
            "objection_index",
            name="uq_fm_obj_state_per_objection",
        ),
    )
    op.create_index(
        "ix_fm_obj_state_plan",
        "fm_objection_user_state",
        ["plan_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fm_obj_state_plan",
        table_name="fm_objection_user_state",
    )
    op.drop_table("fm_objection_user_state")
