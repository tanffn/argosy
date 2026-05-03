"""phase6: productization — users.email, users.plan, tenants, setup_tokens.

Revision ID: 0007_phase6
Revises: 0006_phase5
Create Date: 2026-05-02

Phase 6 introduces multi-tenant productization:

  - `users.email` TEXT, indexed, nullable. NextAuth maps the email claim
    on a JWT to a user_id via this column.
  - `users.plan` TEXT, default "free". Caches the entitlements tier so
    we don't have to re-load the YAML on every request.
  - `tenants` table — registry of provisioned tenants. One row per
    `user_id`. `db_path` is informational (the engine factory derives
    it from `${ARGOSY_HOME}/tenants/<user_id>/argosy.db`).
  - `setup_tokens` table — single-use tokens for first-time login during
    onboarding. Created by `argosy admin tenant create`; consumed by the
    NextAuth credentials provider.

Design choice: tenants + setup_tokens live in the *control* DB (the one
ARGOSY_HOME points at). Per-tenant DBs do not contain these tables. This
keeps the cross-tenant registry one-stop while still isolating tenant
operational data per-DB.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_phase6"
down_revision: Union[str, Sequence[str], None] = "0006_phase5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # users: add email + plan
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("email", sa.String(length=255), nullable=True))
        batch.add_column(
            sa.Column(
                "plan", sa.String(length=32), nullable=False, server_default="free"
            )
        )
    op.create_index("ix_users_email", "users", ["email"])

    # tenants: control-plane registry
    op.create_table(
        "tenants",
        sa.Column("user_id", sa.String(length=64), primary_key=True),
        sa.Column("db_path", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("plan", sa.String(length=32), nullable=False, server_default="free"),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="active"
        ),
    )

    # setup_tokens: one-time first-login tokens
    op.create_table(
        "setup_tokens",
        sa.Column("token", sa.String(length=128), primary_key=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_setup_tokens_user_id", "setup_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_setup_tokens_user_id", table_name="setup_tokens")
    op.drop_table("setup_tokens")
    op.drop_table("tenants")
    op.drop_index("ix_users_email", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("plan")
        batch.drop_column("email")
