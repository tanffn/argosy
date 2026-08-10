"""observed_decision — durable source_decision_run_id + run-unique idempotency.

Sol review of the fleet→ledger recording bridge (defect: idempotency was keyed on
``birth_input_fingerprint``, which embeds the snapshot content-hash — so a retry of
the SAME fleet run AFTER a new snapshot arrived produced a DIFFERENT fingerprint and
a DUPLICATE observation, and the pre-check had NO backing DB constraint so a
concurrent retry raced two inserts).

Fix: the IDENTITY of a per-ticker decision is the fleet RUN, not the book it was
authored against. This migration adds a durable ``source_decision_run_id`` column and
a **partial UNIQUE index** on ``(user_id, subject, decision_kind,
source_decision_run_id)`` (partial: only where the run id is NOT NULL, so the
fingerprint-fallback path for run-less decisions is not constrained). The recorder
passes the fleet ``decision_run_id`` and, on the unique conflict, re-selects and
returns the existing observation — race-safe, exactly-once per run, independent of
snapshot-hash churn. ``birth_input_fingerprint`` stays the INPUT commitment (for
promotion matching), NOT the identity.

This migration does NOT touch the live db/argosy.db.

Revision ID: 0100_observed_decision_run_id
Revises: 0099_decision_ledger
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0100_observed_decision_run_id"
down_revision: str | None = "0099_decision_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD COLUMN does not fire the BEFORE UPDATE immutability trigger (SQLite), so
    # the append-only guard on observed_decision is unaffected.
    op.add_column(
        "observed_decision",
        sa.Column("source_decision_run_id", sa.Integer(), nullable=True),
    )
    # Partial unique index — run identity is enforced ONLY when a run id is present.
    # Run-less decisions (fingerprint fallback) are not DB-constrained here.
    op.create_index(
        "uq_observed_decision_run",
        "observed_decision",
        ["user_id", "subject", "decision_kind", "source_decision_run_id"],
        unique=True,
        sqlite_where=sa.text("source_decision_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_observed_decision_run", table_name="observed_decision")
    op.drop_column("observed_decision", "source_decision_run_id")
