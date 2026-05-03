"""phase4: audit_log, lots, fills, pending_orders.

Revision ID: 0005_phase4
Revises: 0004_phase3
Create Date: 2026-05-02

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_phase4"
down_revision: Union[str, Sequence[str], None] = "0004_phase3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # audit_log — universal append-only event log (SDD §14.1)
    # ------------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("entity_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"])
    op.create_index("ix_audit_log_event_type", "audit_log", ["event_type"])
    op.create_index("ix_audit_log_entity_type", "audit_log", ["entity_type"])
    op.create_index("ix_audit_log_entity_id", "audit_log", ["entity_id"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    # ------------------------------------------------------------------
    # lots — per-tax-lot cost basis (SDD §9.1)
    # ------------------------------------------------------------------
    op.create_table(
        "lots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("lot_id_external", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("cost_basis_usd", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_lots_user_id", "lots", ["user_id"])
    op.create_index("ix_lots_account_id", "lots", ["account_id"])
    op.create_index("ix_lots_ticker", "lots", ["ticker"])

    # ------------------------------------------------------------------
    # fills — per-execution event log
    # ------------------------------------------------------------------
    op.create_table(
        "fills",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "proposal_id",
            sa.Integer(),
            sa.ForeignKey("proposals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("broker", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "broker_order_id", sa.String(length=128), nullable=False, server_default=""
        ),
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("price", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("commission", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column(
            "filled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("paper", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index("ix_fills_user_id", "fills", ["user_id"])
    op.create_index("ix_fills_proposal_id", "fills", ["proposal_id"])
    op.create_index("ix_fills_broker_order_id", "fills", ["broker_order_id"])
    op.create_index("ix_fills_ticker", "fills", ["ticker"])
    op.create_index("ix_fills_filled_at", "fills", ["filled_at"])
    op.create_index("ix_fills_paper", "fills", ["paper"])

    # ------------------------------------------------------------------
    # pending_orders — open broker orders awaiting reconciliation
    # ------------------------------------------------------------------
    op.create_table(
        "pending_orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "proposal_id",
            sa.Integer(),
            sa.ForeignKey("proposals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("broker", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "broker_order_id", sa.String(length=128), nullable=False, server_default=""
        ),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="submitted"
        ),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index("ix_pending_orders_user_id", "pending_orders", ["user_id"])
    op.create_index("ix_pending_orders_proposal_id", "pending_orders", ["proposal_id"])
    op.create_index(
        "ix_pending_orders_broker_order_id", "pending_orders", ["broker_order_id"]
    )
    op.create_index("ix_pending_orders_status", "pending_orders", ["status"])


def downgrade() -> None:
    op.drop_index("ix_pending_orders_status", table_name="pending_orders")
    op.drop_index("ix_pending_orders_broker_order_id", table_name="pending_orders")
    op.drop_index("ix_pending_orders_proposal_id", table_name="pending_orders")
    op.drop_index("ix_pending_orders_user_id", table_name="pending_orders")
    op.drop_table("pending_orders")

    op.drop_index("ix_fills_paper", table_name="fills")
    op.drop_index("ix_fills_filled_at", table_name="fills")
    op.drop_index("ix_fills_ticker", table_name="fills")
    op.drop_index("ix_fills_broker_order_id", table_name="fills")
    op.drop_index("ix_fills_proposal_id", table_name="fills")
    op.drop_index("ix_fills_user_id", table_name="fills")
    op.drop_table("fills")

    op.drop_index("ix_lots_ticker", table_name="lots")
    op.drop_index("ix_lots_account_id", table_name="lots")
    op.drop_index("ix_lots_user_id", table_name="lots")
    op.drop_table("lots")

    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_entity_id", table_name="audit_log")
    op.drop_index("ix_audit_log_entity_type", table_name="audit_log")
    op.drop_index("ix_audit_log_event_type", table_name="audit_log")
    op.drop_index("ix_audit_log_user_id", table_name="audit_log")
    op.drop_table("audit_log")
