"""phase4 (investor events): investor_events.

Revision ID: 0012_investor_events
Revises: 0011_rename_prices_cache_to_kv_cache
Create Date: 2026-05-04

Adds the ``investor_events`` table — durable storage for the structured
events the Phase 4 adapters (SEC Form 4, 13F, TipRanks, CapitolTrades)
emit on each pull. Daily-brief gather persists rows here so the home
brief's signal bullet can surface the most-recent investor event from a
single ``ORDER BY occurred_at DESC LIMIT 1`` query — no coupling to
``kv_cache`` TTL boundaries.

Schema:
  - ``id``               surrogate key
  - ``user_id``          owner (FK users)
  - ``ticker``           issuer ticker; NULL for filer-level / non-equity rows
  - ``source``           ``sec_form4`` / ``sec_13f`` / ``tipranks`` / ``capitoltrades``
  - ``event_kind``       short label (e.g. ``insider_purchase``, ``13f_filing``)
  - ``headline``         human-readable one-liner for the signal bullet
  - ``occurred_at``      event time (transaction date, filing date, etc.)
  - ``ingested_at``      row write time
  - ``payload_json``     full structured payload from the adapter

Indexes: ``(user_id, occurred_at)`` for the home-brief query;
``(user_id, source, ticker)`` for future per-source / per-ticker drilldowns.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_investor_events"
down_revision: Union[str, Sequence[str], None] = "0011_rename_prices_cache_to_kv_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "investor_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("event_kind", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("headline", sa.Text(), nullable=False, server_default=""),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_investor_events_user_occurred",
        "investor_events",
        ["user_id", "occurred_at"],
    )
    op.create_index(
        "ix_investor_events_user_source_ticker",
        "investor_events",
        ["user_id", "source", "ticker"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_investor_events_user_source_ticker",
        table_name="investor_events",
    )
    op.drop_index(
        "ix_investor_events_user_occurred",
        table_name="investor_events",
    )
    op.drop_table("investor_events")
