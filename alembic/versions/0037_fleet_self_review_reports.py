"""fleet_self_review_reports: persisted output of the FleetSelfReviewAgent.

Revision ID: 0037_fleet_self_review_reports
Revises: 0036_fm_objection_user_state
Create Date: 2026-05-26

Adds the ``fleet_self_review_reports`` table.  One row per self-review
run — fired automatically by either:

  * ``scope_kind='post_synthesis'`` — after every ``decision_runs``
    plan_revision completion (orchestrator hook); ``decision_run_id``
    points at the synthesis run that just finished.
  * ``scope_kind='daily'``         — daily sweep alongside the daily
    brief (gated by ``ARGOSY_DAILY_BRIEF_ENABLED=1``);
    ``decision_run_id`` is NULL.

``content_md`` is the human-readable markdown report.  ``findings_json``
is the structured list of ``Finding`` dataclasses for the UI /
programmatic consumers.  ``severity_summary_json`` is a tiny pre-joined
``{"RED": N, "AMBER": M, "YELLOW": K}`` so the home-page badge doesn't
have to parse ``findings_json`` on every request.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_fleet_self_review_reports"
down_revision: str | None = "0036_fm_objection_user_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fleet_self_review_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("scope_kind", sa.String(length=32), nullable=False),
        sa.Column(
            "decision_run_id",
            sa.Integer(),
            sa.ForeignKey("decision_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("content_md", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "findings_json", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "severity_summary_json",
            sa.Text(),
            nullable=False,
            server_default='{"RED":0,"AMBER":0,"YELLOW":0}',
        ),
        sa.CheckConstraint(
            "scope_kind IN ('post_synthesis','daily','manual')",
            name="ck_fleet_self_review_scope_kind",
        ),
    )
    op.create_index(
        "ix_fleet_self_review_user_generated",
        "fleet_self_review_reports",
        ["user_id", "generated_at"],
    )
    op.create_index(
        "ix_fleet_self_review_decision_run",
        "fleet_self_review_reports",
        ["decision_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fleet_self_review_decision_run",
        table_name="fleet_self_review_reports",
    )
    op.drop_index(
        "ix_fleet_self_review_user_generated",
        table_name="fleet_self_review_reports",
    )
    op.drop_table("fleet_self_review_reports")
