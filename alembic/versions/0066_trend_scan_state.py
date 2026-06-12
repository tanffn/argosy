"""trend_scan_state — smart-refresh memory for the high-potential discovery funnel.

Revision ID: 0066_trend_scan_state
Revises: 0065_plan_sections_json
Create Date: 2026-06-12

Phase 2 (discovery funnel). One row per (user, ticker): the last radar score +
a ``radar_fingerprint`` (score/families/liquidity) for diffing, the cached
estimator/fleet verdicts as JSON, a ``status`` (active / quarantined / dropped)
+ ``rank`` + ``quarantine_reason``, and per-stage timestamps. The funnel diffs
against this to re-estimate/re-grade only new or materially-changed names and to
TTL-evict names that fall off the radar.

JSON columns are Text + a nullable-tolerant ``json_valid`` CHECK (mirrors
migration 0049). Composite PK ``(user_id, ticker)``. A covering index on
``(user_id, status)`` serves the GET path (active rows for a user). Real
downgrade drops the table.

SQLite note: ``json_valid`` requires SQLite >= 3.38 (Argosy baseline).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066_trend_scan_state"
down_revision: str | None = "0065_plan_sections_json"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trend_scan_state",
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("ticker", sa.String(32), primary_key=True, nullable=False),
        sa.Column("last_score", sa.Float, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column("rank", sa.Integer, nullable=True),
        sa.Column(
            "quarantine_reason", sa.Text, nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "radar_fingerprint", sa.Text, nullable=False, server_default=sa.text("''")
        ),
        sa.Column("estimator_json", sa.Text, nullable=True),
        sa.Column("fleet_json", sa.Text, nullable=True),
        sa.Column("last_radar_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_estimated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fleet_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
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
            "estimator_json IS NULL OR json_valid(estimator_json)",
            name="ck_trend_scan_state_estimator_json_valid",
        ),
        sa.CheckConstraint(
            "fleet_json IS NULL OR json_valid(fleet_json)",
            name="ck_trend_scan_state_fleet_json_valid",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'quarantined', 'dropped')",
            name="ck_trend_scan_state_status",
        ),
    )
    op.create_index(
        "ix_trend_scan_state_user_status",
        "trend_scan_state",
        ["user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_trend_scan_state_user_status", table_name="trend_scan_state")
    op.drop_table("trend_scan_state")
