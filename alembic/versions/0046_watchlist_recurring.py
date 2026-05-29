"""watchlist_observations + recurring_charge_patterns: Bucket B tables.

Revision ID: 0046_watchlist_recurring
Revises: 0045_merchant_rolling_stats
Create Date: 2026-05-29

Sprint #2 (anomaly detection) commit #2 — backs Bucket B (recurring-pattern
anomalies). See spec
``docs/superpowers/specs/2026-05-29-anomaly-detection-rsu-prevest-design.md``
§1.2 for the design.

Two tables landed together because they share Bucket B concerns:

  * ``watchlist_observations`` — per-statement status log for declared
    watchlist entries (e.g. ``discount_bank_card_2923_fee_waiver``).
    Status enum is a 4-state machine: ``MATCHED``, ``MISSING``, ``PARTIAL``,
    ``UNKNOWN`` (per codex BLOCKER #1 — disambiguates statement-missing
    vs pattern-missing). Pattern B1 (fee-waiver missing) fires only on
    the MATCHED→MISSING transition between two consecutive observation
    periods that both correspond to existing statements.

  * ``recurring_charge_patterns`` — learned recurring-charge patterns
    per merchant. Pattern B2 (recurring-charge missing) fires when an
    active pattern's expected charge window passes (cadence + grace)
    with no match. Status enum: ``active`` / ``dormant`` /
    ``user_dismissed``.

Indexes per spec: lookups happen primarily by user + watchlist entry
(B1) and by user + merchant (B2). Both tables UNIQUE on the natural key
to keep recomputes idempotent.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_watchlist_recurring"
down_revision: str | None = "0045_merchant_rolling_stats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VALID_OBSERVATION_STATUSES = ("MATCHED", "MISSING", "PARTIAL", "UNKNOWN")
_VALID_PATTERN_STATUSES = ("active", "dormant", "user_dismissed")


def upgrade() -> None:
    # ---- watchlist_observations ----------------------------------------
    op.create_table(
        "watchlist_observations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("watchlist_entry_id", sa.String(128), nullable=False),
        sa.Column("observation_period", sa.Date, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "evidence_tx_ids",
            sa.Text,
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ("
            + ", ".join(repr(s) for s in _VALID_OBSERVATION_STATUSES)
            + ")",
            name="ck_watchlist_observations_status",
        ),
        sa.UniqueConstraint(
            "user_id",
            "watchlist_entry_id",
            "observation_period",
            name="uq_watchlist_observations_period",
        ),
    )
    op.create_index(
        "ix_watchlist_observations_user_entry",
        "watchlist_observations",
        ["user_id", "watchlist_entry_id", "observation_period"],
    )

    # ---- recurring_charge_patterns -------------------------------------
    op.create_table(
        "recurring_charge_patterns",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("merchant_normalized", sa.String(512), nullable=False),
        sa.Column(
            "expected_amount_nis", sa.Numeric(12, 2), nullable=False
        ),
        sa.Column(
            "amount_tolerance",
            sa.Numeric(4, 3),
            nullable=False,
            server_default="0.15",
        ),
        sa.Column("cadence_days", sa.Integer, nullable=False),
        sa.Column(
            "cadence_tolerance_days",
            sa.Integer,
            nullable=False,
            server_default="7",
        ),
        sa.Column("first_seen", sa.Date, nullable=False),
        sa.Column("last_seen", sa.Date, nullable=False),
        sa.Column("occurrence_count", sa.Integer, nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ("
            + ", ".join(repr(s) for s in _VALID_PATTERN_STATUSES)
            + ")",
            name="ck_recurring_charge_patterns_status",
        ),
        sa.CheckConstraint(
            "expected_amount_nis > 0",
            name="ck_recurring_charge_patterns_amount_positive",
        ),
        sa.CheckConstraint(
            "cadence_days > 0",
            name="ck_recurring_charge_patterns_cadence_positive",
        ),
        sa.CheckConstraint(
            "occurrence_count >= 3",
            name="ck_recurring_charge_patterns_min_occurrences",
        ),
        sa.UniqueConstraint(
            "user_id",
            "merchant_normalized",
            "expected_amount_nis",
            name="uq_recurring_charge_patterns_merchant",
        ),
    )
    op.create_index(
        "ix_recurring_charge_patterns_user_merchant",
        "recurring_charge_patterns",
        ["user_id", "merchant_normalized"],
    )
    op.create_index(
        "ix_recurring_charge_patterns_active",
        "recurring_charge_patterns",
        ["user_id", "last_seen"],
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recurring_charge_patterns_active",
        table_name="recurring_charge_patterns",
    )
    op.drop_index(
        "ix_recurring_charge_patterns_user_merchant",
        table_name="recurring_charge_patterns",
    )
    op.drop_table("recurring_charge_patterns")

    op.drop_index(
        "ix_watchlist_observations_user_entry",
        table_name="watchlist_observations",
    )
    op.drop_table("watchlist_observations")
