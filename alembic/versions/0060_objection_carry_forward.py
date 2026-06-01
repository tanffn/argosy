"""objection_carry_forward — add audit fields to fm_objection_user_state.

Revision ID: 0060_objection_carry_forward
Revises: 0059_pending_reevaluations
Create Date: 2026-06-01

Wave 7 Piece B — stance carry-forward across drafts. When a new
synthesis draft commits, the carry-forward matcher attempts to
identify each new-draft FM objection as a continuation of a
prior-draft objection (the user already resolved it). Match found
→ the prior stance + counter-position is threaded into the new
draft's FM + triage prompts so the FM can't silently re-raise
something the user has already answered.

This migration adds five audit columns so every match decision is
inspectable + tunable:

  - ``matched_from_plan_version_id``  the prior draft's
    plan_version_id whose row was carried forward into this row.
    NULL when the row was created fresh by the user (no carry-
    forward) — preserving the existing semantics.
  - ``match_kind``  'exact_hash' | 'embedding' | NULL. Tells the
    audit log which leg of the matching stack accepted the match.
  - ``match_score``  the matcher's confidence on this row.
    1.0 for exact_hash (no embedding needed). Cosine similarity
    in [0,1] for embedding matches. NULL when this row was
    created fresh.
  - ``match_top2_score``  for embedding matches only, the score
    of the SECOND-best candidate. The matcher's ambiguity guard
    requires (match_score - match_top2_score) >= 0.05. Persisted
    so calibration data is recoverable. NULL otherwise.
  - ``embedding_model``  the model identifier (e.g.
    'sentence-transformers/all-MiniLM-L6-v2') for embedding
    matches. NULL when match_kind is exact_hash or NULL.
  - ``embedding_model_version``  the model's version string
    (e.g. semver of sentence-transformers + a content-hash of
    the model weights) so future swaps don't silently change
    score semantics.

Downgrade drops every added column. Existing rows are not
affected: pre-wave-7 rows carry NULL in every new column.

SQLite note: ``ALTER TABLE ADD COLUMN`` is supported on every
supported SQLite (>= 3.38); no batch migration needed.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0060_objection_carry_forward"
down_revision: str | None = "0059_pending_reevaluations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VALID_MATCH_KINDS: tuple[str, ...] = ("exact_hash", "embedding")


def upgrade() -> None:
    op.add_column(
        "fm_objection_user_state",
        sa.Column("matched_from_plan_version_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "fm_objection_user_state",
        sa.Column("match_kind", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "fm_objection_user_state",
        sa.Column("match_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "fm_objection_user_state",
        sa.Column("match_top2_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "fm_objection_user_state",
        sa.Column("embedding_model", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "fm_objection_user_state",
        sa.Column("embedding_model_version", sa.String(length=64), nullable=True),
    )
    # CHECK constraint on match_kind. SQLite batch-mode needed because
    # the table already exists and SQLite cannot add a CHECK constraint
    # via ALTER TABLE.
    with op.batch_alter_table("fm_objection_user_state") as batch_op:
        batch_op.create_check_constraint(
            "ck_fm_obj_state_match_kind",
            f"match_kind IS NULL OR match_kind IN "
            f"({', '.join(repr(k) for k in _VALID_MATCH_KINDS)})",
        )


def downgrade() -> None:
    with op.batch_alter_table("fm_objection_user_state") as batch_op:
        batch_op.drop_constraint(
            "ck_fm_obj_state_match_kind", type_="check"
        )
    op.drop_column("fm_objection_user_state", "embedding_model_version")
    op.drop_column("fm_objection_user_state", "embedding_model")
    op.drop_column("fm_objection_user_state", "match_top2_score")
    op.drop_column("fm_objection_user_state", "match_score")
    op.drop_column("fm_objection_user_state", "match_kind")
    op.drop_column("fm_objection_user_state", "matched_from_plan_version_id")
