"""position_stances — ONE canonical stance record per held position.

Stance-registry build (Ariel-directed, 2026-07-10): /portfolio said HOLD while
the fleet review said SELL and the inbox held a proposal for the same ticker
(three voices, SPCX). Every surface now projects one reconciled row with a
fixed precedence: open proposal > verified review (outcome 'proposed'/'hold')
> plan stance. ``held_unverified`` reviews set ``divergence`` + a
nothing-hidden note but never change the stance (fail-closed).

Additive only; NO data backfill — rows are a projection rebuilt on demand
(delete+insert per user) by ``argosy/services/position_stance.py``.

Revision ID: 0080_position_stances
Revises: 0079_holding_reviews
Create Date: 2026-07-10
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0080_position_stances"
down_revision: str | None = "0079_holding_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "position_stances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("stance", sa.String(8), nullable=False),
        sa.Column("stance_source", sa.String(16), nullable=False),
        sa.Column("conviction", sa.String(16), nullable=False),
        sa.Column("plan_verdict", sa.String(16), nullable=True),
        sa.Column("review_verdict", sa.String(16), nullable=True),
        sa.Column("review_outcome", sa.String(32), nullable=True),
        sa.Column(
            "pending_proposal_id",
            sa.Integer(),
            sa.ForeignKey("proposals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "divergence",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("falsifiers_json", sa.Text(), nullable=True),
        sa.Column("reasoning_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("plan_version_id", sa.Integer(), nullable=True),
        sa.Column("snapshot_key", sa.String(128), nullable=True),
        sa.Column(
            "built_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "user_id", "symbol", name="uq_position_stances_user_symbol"
        ),
    )


def downgrade() -> None:
    op.drop_table("position_stances")
