"""spine integrity floor — integrity_verdict + integrity_verdict_head.

PHASE 1 of the operating-model spine (docs/design/argosy_operating_model_spec.md
§2A/§3). Two append-only-friendly tables that hold the per-snapshot conservation
verdict and the single authoritative (current) verdict per snapshot:

  * ``integrity_verdict``      — one immutable pass/fail row per evaluation,
    committing to the exact normalized snapshot bytes it assessed
    (``snapshot_content_hash``) and ordered by a monotonic ``verdict_seq``.
  * ``integrity_verdict_head`` — one row per ``snapshot_id`` (PK), CAS-advanced
    to the current verdict so a later ``fail`` demotes a prior ``pass``.

No trigger / consumer rewiring here — this is the producer + head only (spec
scope note: the validated_snapshot trigger and the ~19-reader cut-over land in
later slices). This migration does NOT touch the live db/argosy.db.

Revision ID: 0098_spine_integrity
Revises: 0097_unmanaged_holdings
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0098_spine_integrity"
down_revision: str | None = "0097_unmanaged_holdings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integrity_verdict",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("portfolio_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("result", sa.String(8), nullable=False),
        sa.Column("snapshot_content_hash", sa.String(64), nullable=False),
        sa.Column("verdict_seq", sa.Integer(), nullable=False),
        # Provenance is MANDATORY (spec §3): a provenance-free pass is forbidden.
        sa.Column("threshold_policy_version", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("detail_json", sa.Text(), nullable=False),
        sa.Column(
            "authored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "result IN ('pass','fail')", name="ck_integrity_verdict_result"
        ),
        sa.UniqueConstraint(
            "snapshot_id", "verdict_seq", name="uq_integrity_verdict_snapshot_seq"
        ),
        # Composite-unique target for the head's composite FK (defect 4).
        sa.UniqueConstraint(
            "snapshot_id", "id", name="uq_integrity_verdict_snapshot_id"
        ),
    )
    op.create_index(
        "ix_integrity_verdict_snapshot", "integrity_verdict", ["snapshot_id"]
    )
    op.create_index("ix_integrity_verdict_user", "integrity_verdict", ["user_id"])

    op.create_table(
        "integrity_verdict_head",
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("portfolio_snapshots.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("current_verdict_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        # Composite FK — the head may ONLY point at a verdict of the SAME
        # snapshot; the DB refuses a cross-snapshot head (defect 4).
        sa.ForeignKeyConstraint(
            ["snapshot_id", "current_verdict_id"],
            ["integrity_verdict.snapshot_id", "integrity_verdict.id"],
            name="fk_integrity_verdict_head_same_snapshot",
        ),
    )


def downgrade() -> None:
    op.drop_table("integrity_verdict_head")
    op.drop_index("ix_integrity_verdict_user", table_name="integrity_verdict")
    op.drop_index("ix_integrity_verdict_snapshot", table_name="integrity_verdict")
    op.drop_table("integrity_verdict")
