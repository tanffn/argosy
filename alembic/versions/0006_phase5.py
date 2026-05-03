"""phase5: argonaut_snapshots, daily_account_pnl, totp_secrets.

Revision ID: 0006_phase5
Revises: 0005_phase4
Create Date: 2026-05-02

Phase 5 introduces the Argonaut limited-account autonomy stack:

  - `argonaut_snapshots` — daily P&L curve since inception
  - `daily_account_pnl` — per-account, per-day realized + unrealized P&L
                           rollup (drives the daily-loss-limit gate)
  - `totp_secrets`       — per-user TOTP secret (encrypted text) for the
                           T3 second-factor flow
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_phase5"
down_revision: Union[str, Sequence[str], None] = "0005_phase4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # argonaut_snapshots — one row per (user, account, date)
    # ------------------------------------------------------------------
    op.create_table(
        "argonaut_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("account_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("date", sa.String(length=10), nullable=False),  # YYYY-MM-DD
        sa.Column("total_value_usd", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("cash_usd", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column(
            "positions_value_usd", sa.Numeric(18, 4), nullable=False, server_default="0"
        ),
        sa.Column("day_pnl_usd", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "user_id", "account_id", "date", name="uq_argonaut_snapshots_user_acct_date"
        ),
    )
    op.create_index(
        "ix_argonaut_snapshots_user_id", "argonaut_snapshots", ["user_id"]
    )
    op.create_index(
        "ix_argonaut_snapshots_account_id", "argonaut_snapshots", ["account_id"]
    )
    op.create_index("ix_argonaut_snapshots_date", "argonaut_snapshots", ["date"])

    # ------------------------------------------------------------------
    # daily_account_pnl — drives the per-account daily-loss-limit gate
    # ------------------------------------------------------------------
    op.create_table(
        "daily_account_pnl",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("account_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("date", sa.String(length=10), nullable=False),  # YYYY-MM-DD
        sa.Column(
            "realized_pnl_usd", sa.Numeric(18, 4), nullable=False, server_default="0"
        ),
        sa.Column(
            "unrealized_pnl_usd", sa.Numeric(18, 4), nullable=False, server_default="0"
        ),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "user_id", "account_id", "date", name="uq_daily_account_pnl_user_acct_date"
        ),
    )
    op.create_index(
        "ix_daily_account_pnl_user_id", "daily_account_pnl", ["user_id"]
    )
    op.create_index(
        "ix_daily_account_pnl_account_id", "daily_account_pnl", ["account_id"]
    )
    op.create_index("ix_daily_account_pnl_date", "daily_account_pnl", ["date"])

    # ------------------------------------------------------------------
    # totp_secrets — per-user TOTP secret for T3 second-factor
    # ------------------------------------------------------------------
    op.create_table(
        "totp_secrets",
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("secret_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("totp_secrets")

    op.drop_index("ix_daily_account_pnl_date", table_name="daily_account_pnl")
    op.drop_index("ix_daily_account_pnl_account_id", table_name="daily_account_pnl")
    op.drop_index("ix_daily_account_pnl_user_id", table_name="daily_account_pnl")
    op.drop_table("daily_account_pnl")

    op.drop_index("ix_argonaut_snapshots_date", table_name="argonaut_snapshots")
    op.drop_index("ix_argonaut_snapshots_account_id", table_name="argonaut_snapshots")
    op.drop_index("ix_argonaut_snapshots_user_id", table_name="argonaut_snapshots")
    op.drop_table("argonaut_snapshots")
