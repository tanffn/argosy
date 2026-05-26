"""portfolio_snapshots: persisted snapshots of the parsed Family Finances TSV.

Revision ID: 0030_portfolio_snapshots
Revises: 0029_agent_reports_prompts
Create Date: 2026-05-26

Removes the filesystem-walk fragility: ``/api/portfolio/snapshot`` and
the synthesis input assembler currently call ``_find_latest_tsv()`` +
``parse_portfolio_tsv()`` on every request. With this table the latest
parsed snapshot is persisted on ingest, so:

- Synthesis Phase 1 inputs read from the DB (deterministic, fast)
- The "this morning's $0k bug" failure mode (stray uploads shadow the
  real TSV) is impossible because the writer validates header marker
  before persisting

Columns mirror the PortfolioSnapshot pydantic shape produced by
argosy.ingest.tsv:

- ``positions_json``: list[dict] (one per holding)
- ``allocations_json``: list[dict] (allocation rows from TSV)
- ``nvda_sales_json``: list[dict] (historical NVDA sales)
- ``totals_json``: {total_usd_value_k, cash_balances_usd_k, ...}
- ``snapshot_date``: the date the TSV represents (not the ingest date)
- ``imported_at``: when this snapshot row was written (UTC)
- ``source_path``: filesystem path of the TSV the snapshot was parsed from

Multi-user safe: ``user_id`` indexed; the freshest snapshot per user is
``ORDER BY imported_at DESC LIMIT 1`` with the where clause.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_portfolio_snapshots"
down_revision: str | None = "0029_agent_reports_prompts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("snapshot_date", sa.Date(), nullable=True),
        sa.Column("imported_at", sa.DateTime(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("positions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("allocations_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("nvda_sales_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("real_estate_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("pensions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("totals_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("fx_usd_nis", sa.Float(), nullable=True),
        sa.Column("fx_usd_eur", sa.Float(), nullable=True),
        sa.Column("parse_warnings_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.create_index(
        "ix_portfolio_snapshots_user_imported",
        "portfolio_snapshots",
        ["user_id", "imported_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_portfolio_snapshots_user_imported", "portfolio_snapshots")
    op.drop_table("portfolio_snapshots")
