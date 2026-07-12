"""Add expense_sources.login_url — issuer login quick-link for uploads.

Additive only. Owner-facing convenience: the upload card renders each
source's issuer login page as a quick link so statement pulls start from
one place. Values are tenant DATA (set via UI/DB), never seeded here.

Revision ID: 0090_expense_source_login_url
Revises: 0089_synthesis_reliability_flags
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0090_expense_source_login_url"
down_revision = "0089_synthesis_reliability_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "expense_sources",
        sa.Column("login_url", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("expense_sources", "login_url")
