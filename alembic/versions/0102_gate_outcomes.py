"""gate_outcomes — persist promotion gate receipts per synthesis run.

Adds the ``gate_outcomes`` table so Ariel can always see whether each
promotion gate (whole_artifact_reader, codex_math, …) actually ran or
was skipped. A DID_NOT_RUN row is NOT the same as a PASS — this table
makes the tri-state visible and non-forgeable.

One row per (decision_run_id, gate) pair; the synthesizer writes these
best-effort at the point _promotion_gates is evaluated.  The /api/plan/draft
route reads them and returns a ``gate_receipt`` field so the /plan page can
render "5/6 gates passed; whole_artifact_reader DID_NOT_RUN (codex timeout)".

This migration does NOT touch the live db/argosy.db.

Revision ID: 0102_gate_outcomes
Revises: 0101_fill_verdict_link
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0102_gate_outcomes"
down_revision: str | None = "0101_fill_verdict_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gate_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("decision_run_id", sa.Integer(), nullable=False),
        sa.Column("gate", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("override_by", sa.String(128), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("meta_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "decision_run_id", "gate", name="uq_gate_outcomes_run_gate"
        ),
    )
    op.create_index(
        "ix_gate_outcomes_decision_run_id",
        "gate_outcomes",
        ["decision_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_gate_outcomes_decision_run_id", table_name="gate_outcomes")
    op.drop_table("gate_outcomes")
