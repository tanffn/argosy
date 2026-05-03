"""drop orphan user_context.id

Revision ID: 0009_drop_orphan_user_context_id
Revises: 0008_intake_session
Create Date: 2026-05-03

The very early development DB had an extra `id INTEGER NOT NULL` column
on `user_context` that was never represented in any migration or in the
SQLAlchemy model. The column has no default, so any INSERT of a fresh
`user_context` row from production code (which doesn't supply `id`)
fails with `NOT NULL constraint failed: user_context.id`.

The fix is to drop the orphan column. Since `user_id` is the primary
key, dropping `id` loses no information.

This is idempotent: if the column doesn't exist (clean DB built from
the model + migrations from 0001), the upgrade is a no-op.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_drop_orphan_user_context_id"
down_revision: Union[str, Sequence[str], None] = "0008_intake_session"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_id_column() -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("user_context")}
    return "id" in cols


def upgrade() -> None:
    if not _has_id_column():
        return  # clean DB — nothing to drop
    with op.batch_alter_table("user_context") as batch:
        batch.drop_column("id")


def downgrade() -> None:
    # The orphan column had no semantic meaning; we don't recreate it.
    # If a downgrade lands on a DB that previously had the column, it
    # will be re-added without a default, matching the prior shape.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("user_context")}
    if "id" in cols:
        return
    with op.batch_alter_table("user_context") as batch:
        batch.add_column(sa.Column("id", sa.Integer(), nullable=True))
