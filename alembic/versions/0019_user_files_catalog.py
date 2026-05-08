"""user_files catalog table + plan_versions.source_file_id FK (Wave A — provenance).

Revision ID: 0019_user_files_catalog
Revises: 0018_decision_runs_amendment
Create Date: 2026-05-08

Wave A introduces a single boundary helper (``argosy/services/file_catalog.py``)
through which every user-supplied file flows: chat attachments, intake plan
imports, intake file-to-text conversions, and broker CSV imports. Today those
paths each write to disk independently, with no DB row tying them back to a
user, a turn, or a downstream decision. ``user_files`` is the catalog: one row
per stored byte-blob per user, with a sha256-based partial unique index so
re-uploads of the same content collapse into a single row instead of growing
disk forever.

Schema notes:

- ``sha256`` + ``user_id`` form the dedup key. The partial unique index
  ``WHERE deleted_at IS NULL`` lets a user soft-delete a file and re-upload
  the identical bytes later without colliding with the tombstone row.
- ``storage_path`` is the absolute path on disk; the new layout is
  ``<ARGOSY_HOME>/uploads/<user_id>/<YYYY>/<YYYY-MM-DD>/<HHMMSS>__<sha8>__<sanitized>``.
  Existing Wave 5 chat-upload paths under
  ``<ARGOSY_HOME>/uploads/<user_id>/<turn_uuid>/<file>`` are NOT migrated by
  this revision — the backfill CLI (``argosy admin catalog-backfill``) inserts
  rows pointing at those legacy paths so old files remain referenceable.
- ``source`` records the ingest channel:
  ``chat_attachment`` / ``intake_upload`` / ``intake_file_to_text`` / ``cost_basis_import``.
  ``kind`` is the content kind (``text``/``image``/``plan_markdown``/``broker_csv``/``other``).
- ``plan_versions.source_file_id`` links a baseline plan back to its catalog
  row; existing ``plan_versions.source_path`` (a string filename) stays in
  place for back-compat with any consumer that doesn't yet read the FK.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_user_files_catalog"
down_revision: str | Sequence[str] | None = "0018_decision_runs_amendment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=512), nullable=False),
        sa.Column("sanitized_name", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("turn_uuid", sa.String(length=64), nullable=True),
        sa.Column("intake_session_id", sa.String(length=64), nullable=True),
        sa.Column(
            "plan_version_id",
            sa.Integer(),
            sa.ForeignKey("plan_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "decision_run_id",
            sa.Integer(),
            sa.ForeignKey("decision_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Per-user file listing index (drives GET /api/files).
    op.create_index(
        "ix_user_files_user_created",
        "user_files",
        ["user_id", sa.text("created_at DESC")],
    )
    # Cross-user content lookup (used by the backfill / dedup helper).
    op.create_index(
        "ix_user_files_sha256",
        "user_files",
        ["sha256"],
    )
    # Optional join surface for intake sessions.
    op.create_index(
        "ix_user_files_intake_session_id",
        "user_files",
        ["intake_session_id"],
    )
    # Per-user content-addressed dedup. Partial unique on (user_id, sha256)
    # WHERE deleted_at IS NULL — a soft-deleted row leaves space for the
    # same bytes to be re-uploaded later. Same partial-index pattern as
    # 0018's ix_decision_runs_one_amendment_running_per_user (see
    # alembic/versions/0018_decision_runs_amendment.py:58-69).
    op.create_index(
        "ix_user_files_user_sha256_active",
        "user_files",
        ["user_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )

    # Bridge column on plan_versions so a baseline plan points at its catalog row.
    # Add the column first, then create the FK with an explicit name (batch
    # mode in alembic requires named constraints).
    with op.batch_alter_table("plan_versions") as batch:
        batch.add_column(sa.Column("source_file_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_plan_versions_source_file_id",
            "user_files",
            ["source_file_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("plan_versions") as batch:
        batch.drop_constraint("fk_plan_versions_source_file_id", type_="foreignkey")
        batch.drop_column("source_file_id")

    op.drop_index("ix_user_files_user_sha256_active", table_name="user_files")
    op.drop_index("ix_user_files_intake_session_id", table_name="user_files")
    op.drop_index("ix_user_files_sha256", table_name="user_files")
    op.drop_index("ix_user_files_user_created", table_name="user_files")
    op.drop_table("user_files")
