"""portfolio_snapshot_parts: partial unique index excluding NULL portfolio_number.

Revision ID: 0040_portfolio_snapshot_parts_partial_unique
Revises: 0039_portfolio_snapshot_parts
Create Date: 2026-05-29

Codex zigzag review (a)#8 of commit 198e19c (2026-05-29) flagged that
the SQLite UNIQUE constraint on
``(user_id, snapshot_date, portfolio_number)`` declared in 0039 does
nothing for rows where ``portfolio_number IS NULL`` -- SQLite treats
each NULL as distinct under the UNIQUE semantics, so multiple
"semantically identical" pending parts could coexist when the XLS
parser fails to extract the Leumi portfolio number.

This migration drops the broken full-row UNIQUE constraint and
recreates it as a partial unique index limited to rows with a non-NULL
portfolio_number. The fast-path SHA uniqueness on
``(user_id, sha256)`` is unaffected and continues to protect against
byte-level re-upload.

Path for NULL portfolio_number rows: the application's
``handle_xls_upload`` performs an explicit pre-insert lookup on
``(user_id, snapshot_date, portfolio_number)`` (SQLAlchemy translates
the None comparison to ``IS NULL``), so the dedup still works at the
application layer for those rows -- this migration removes the
illusion of DB-enforced uniqueness for NULL cases.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_portfolio_snapshot_parts_partial_unique"
down_revision: str | None = "0039_portfolio_snapshot_parts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("portfolio_snapshot_parts") as batch:
        batch.drop_constraint(
            "uq_portfolio_snapshot_parts_user_date_portfolio",
            type_="unique",
        )
    op.create_index(
        "ix_portfolio_snapshot_parts_user_date_portfolio_nonnull",
        "portfolio_snapshot_parts",
        ["user_id", "snapshot_date", "portfolio_number"],
        unique=True,
        sqlite_where=sa.text("portfolio_number IS NOT NULL"),
        postgresql_where=sa.text("portfolio_number IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_snapshot_parts_user_date_portfolio_nonnull",
        table_name="portfolio_snapshot_parts",
    )
    with op.batch_alter_table("portfolio_snapshot_parts") as batch:
        batch.create_unique_constraint(
            "uq_portfolio_snapshot_parts_user_date_portfolio",
            ["user_id", "snapshot_date", "portfolio_number"],
        )
