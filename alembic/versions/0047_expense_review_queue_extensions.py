"""expense_review_queue: add materiality, dedup_key, bucket columns.

Revision ID: 0047_expense_review_queue_extensions
Revises: 0046_watchlist_recurring
Create Date: 2026-05-29

Sprint #2 (anomaly detection) commit #3 — extends the existing
``expense_review_queue`` table with three new columns for the four-bucket
detector taxonomy. See
``docs/superpowers/specs/2026-05-29-anomaly-detection-rsu-prevest-design.md``
§4 migration 0046 (renumbered to 0047 because sprint #1 claimed 0044).

Columns added:
  * ``materiality`` (TEXT NULL) — severity ladder: ``info`` / ``warning`` /
    ``critical``. Drives icon color in the inline transaction-row badge
    and the AnomalyHighlights card severity sort. NULL on legacy rows.
  * ``dedup_key`` (TEXT NULL) — deterministic stable key per anomaly type
    so re-running the detector over the same input doesn't create
    duplicate review-queue rows. Formulas per pattern documented in spec
    §4 (codex IMPORTANT #3 — version-prefixed ``v1|...`` so future rule
    changes get fresh keys without false suppression).
  * ``bucket`` (TEXT NULL) — categorical: ``amount`` / ``recurring`` /
    ``cache`` / ``duplicate``. Maps to the four detector buckets.

Partial unique index ``ix_expense_review_queue_dedup`` on
``(user_id, dedup_key) WHERE dedup_key IS NOT NULL AND status = 'open'``
enforces idempotency for open anomalies; resolved/dismissed rows with
the same dedup_key are allowed (history retained) so the detector can
re-fire if conditions change after a dismissal.

SQLite ALTER limitation: SQLite cannot ADD a CHECK constraint via plain
ALTER TABLE. Using ``op.batch_alter_table`` to rebuild the table with
the new constraints applied — same pattern as migrations 0040 + 0041.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_expense_review_queue_extensions"
down_revision: str | None = "0046_watchlist_recurring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VALID_MATERIALITIES = ("info", "warning", "critical")
_VALID_BUCKETS = ("amount", "recurring", "cache", "duplicate")


def upgrade() -> None:
    with op.batch_alter_table("expense_review_queue") as batch:
        batch.add_column(
            sa.Column("materiality", sa.String(16), nullable=True)
        )
        batch.add_column(sa.Column("dedup_key", sa.Text, nullable=True))
        batch.add_column(sa.Column("bucket", sa.String(16), nullable=True))
        batch.create_check_constraint(
            "ck_expense_review_queue_materiality",
            "materiality IS NULL OR materiality IN ("
            + ", ".join(repr(m) for m in _VALID_MATERIALITIES)
            + ")",
        )
        batch.create_check_constraint(
            "ck_expense_review_queue_bucket",
            "bucket IS NULL OR bucket IN ("
            + ", ".join(repr(b) for b in _VALID_BUCKETS)
            + ")",
        )

    # Partial unique index: open anomalies sharing a dedup_key are not
    # allowed; resolved/dismissed rows with the same dedup_key are OK
    # (history retention).
    op.create_index(
        "ix_expense_review_queue_dedup",
        "expense_review_queue",
        ["user_id", "dedup_key"],
        unique=True,
        sqlite_where=sa.text("dedup_key IS NOT NULL AND status = 'open'"),
        postgresql_where=sa.text(
            "dedup_key IS NOT NULL AND status = 'open'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_expense_review_queue_dedup",
        table_name="expense_review_queue",
    )
    with op.batch_alter_table("expense_review_queue") as batch:
        batch.drop_constraint(
            "ck_expense_review_queue_bucket", type_="check"
        )
        batch.drop_constraint(
            "ck_expense_review_queue_materiality", type_="check"
        )
        batch.drop_column("bucket")
        batch.drop_column("dedup_key")
        batch.drop_column("materiality")
