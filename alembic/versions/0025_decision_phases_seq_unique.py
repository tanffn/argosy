"""decision_phases (decision_run_id, seq) unique constraint (SDD §17 zigzag fix #3).

Revision ID: 0025_decision_phases_seq_unique
Revises: 0024_expense_transaction_tags
Create Date: 2026-05-15

The §17 zigzag review (2026-05-15) flagged that
``negotiation_recorder.record_negotiation_phase`` computes
``next_seq = max(seq) + 1`` without any locking or DB-level uniqueness
constraint, and the SDD describes the serial-caller contract as a
documented assumption rather than an enforced invariant. Two recorder
calls racing for the same ``decision_run_id`` could:

  * Both compute the same ``next_seq``.
  * Both write the same ``bundle_dir`` (deterministic name from
    ``<run_id>__<kind>``).
  * Both INSERT decision_phases rows with identical ``(run_id, seq)``.
  * Replay endpoint would surface a duplicate phase; bundle on disk
    is now overwritten by whoever wrote last.

This migration adds a unique index on ``(decision_run_id, seq)`` so
the second writer's INSERT raises ``IntegrityError`` and the recorder
can clean up its own bundle without breaking the winner's row.

Promoting the existing non-unique ``ix_decision_phases_run_seq``
index to a unique index covers both the lookup pattern (drives the
Replay endpoint) and the new uniqueness invariant without adding a
separate constraint object.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025_decision_phases_seq_unique"
down_revision: str | Sequence[str] | None = "0024_expense_transaction_tags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "ix_decision_phases_run_seq",
        table_name="decision_phases",
    )
    op.create_index(
        "ix_decision_phases_run_seq",
        "decision_phases",
        ["decision_run_id", "seq"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_decision_phases_run_seq",
        table_name="decision_phases",
    )
    op.create_index(
        "ix_decision_phases_run_seq",
        "decision_phases",
        ["decision_run_id", "seq"],
    )
