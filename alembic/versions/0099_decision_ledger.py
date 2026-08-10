"""spine decision ledger — observed → validated → outcome (three records).

PHASE 2 of the operating-model spine (docs/design/argosy_operating_model_spec.md
§2A "the decision records — OBSERVED → VALIDATED → OUTCOME"). Three append-only /
immutable tables plus the single current-outcome head, mirroring the Phase 1
integrity-verdict / integrity-verdict-head discipline (alembic 0098).

Immutability is enforced at the DB level (Sol review, defect 3): BEFORE UPDATE /
BEFORE DELETE triggers on ``observed_decision`` / ``validated_decision`` /
``validated_decision_outcome`` RAISE(ABORT) — the head table is the ONLY mutable
surface. ``vs_benchmark_delta`` is DEFERRED and CHECK-pinned to NULL (defect 5) —
a later attribution phase derives it from the contribution_ledger. A partial
UNIQUE root index forbids a second disconnected supersession chain (defect 4a).

This migration does NOT touch the live db/argosy.db.

Revision ID: 0099_decision_ledger
Revises: 0098_spine_integrity
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0099_decision_ledger"
down_revision: str | None = "0098_spine_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_IMMUTABLE_TABLES = (
    "observed_decision",
    "validated_decision",
    "validated_decision_outcome",
)


def _create_immutability_triggers() -> None:
    """BEFORE UPDATE / BEFORE DELETE guards — real DB-level append-only (defect 3)."""
    for tbl in _IMMUTABLE_TABLES:
        for op_kind in ("UPDATE", "DELETE"):
            suffix = "no_update" if op_kind == "UPDATE" else "no_delete"
            op.execute(
                f"CREATE TRIGGER trg_{tbl}_{suffix} BEFORE {op_kind} ON {tbl} "
                f"BEGIN SELECT RAISE(ABORT, 'append-only: {tbl} is immutable'); END"
            )


def _drop_immutability_triggers() -> None:
    for tbl in _IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{tbl}_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{tbl}_no_delete")


def upgrade() -> None:
    # -- observed_decision — immutable observation, ALWAYS written ---------------
    op.create_table(
        "observed_decision",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # portfolio-level key or a ticker/symbol/stable-id — the decision's subject.
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("decision_kind", sa.Text(), nullable=False),
        # §2A(a) provenance — conviction at authoring (defect 6). Nullable: not
        # every source supplies it yet, but the column must EXIST.
        sa.Column("conviction", sa.Text(), nullable=True),
        sa.Column(
            "authored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # Durable monotonic per-user watermark, stamped at authoring (NEVER a
        # timestamp) — the decision_manifest closes against this (§2A point 3b).
        sa.Column("ingress_seq", sa.Integer(), nullable=False),
        # The 6 forward terms frozen at birth: an object with EXACTLY the keys
        # {target_band, alternative_at_birth, stop, falsifiers, revisit_triggers,
        # evaluation_due_at}, each a value OR an explicit null (a null is permanent).
        sa.Column("predictive_terms_at_birth", sa.JSON(), nullable=False),
        # "gradable" | "unvalidated:dirty-book" | "unvalidated:missing-predictive-term".
        # Immutable property of the observation AT AUTHORING — never a live signal.
        sa.Column("validation_status_at_birth", sa.Text(), nullable=False),
        # Raw/diagnostic snapshot id when authored on an unvalidated book (NULLABLE).
        sa.Column("observed_source_input_id", sa.Text(), nullable=True),
        # Stable commitment/hash to the input the decision was authored against —
        # the late-attachment identity check reads this (a later book can't attach).
        sa.Column("birth_input_fingerprint", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "user_id", "ingress_seq", name="uq_observed_decision_user_ingress_seq"
        ),
    )
    op.create_index(
        "ix_observed_decision_user", "observed_decision", ["user_id"]
    )
    op.create_index(
        "ix_observed_decision_subject", "observed_decision", ["subject"]
    )

    # -- validated_decision — immutable gradable terms, only when gradable -------
    op.create_table(
        "validated_decision",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "observed_decision_id",
            sa.Integer(),
            sa.ForeignKey("observed_decision.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # A promotion MUST name the validated book it is graded against (defect 1) —
        # NOT NULL. FK to the forthcoming validated_snapshot table (Phase 1);
        # stored as an id ref for now (see model note).
        sa.Column("input_validated_snapshot_id", sa.Text(), nullable=False),
        # §2A(b) provenance columns (defect 6) — nullable where the data is a
        # deferred prerequisite, but the columns must EXIST so records are complete.
        sa.Column("instrument_stable_id", sa.Text(), nullable=True),
        sa.Column("decision_kind", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=True),
        sa.Column("conviction", sa.Text(), nullable=True),
        sa.Column("cost_basis_completeness", sa.Text(), nullable=True),
        sa.Column("metadata_freshness", sa.Text(), nullable=True),
        sa.Column("equivalence_evidence", sa.JSON(), nullable=True),
        # The FULL validated terms (not just the 6 birth keys) — defect 6.
        sa.Column("validated_terms", sa.JSON(), nullable=False),
        sa.Column(
            "authored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # At most one validated_decision per observation (spec §2A(b)).
        sa.UniqueConstraint(
            "observed_decision_id", name="uq_validated_decision_observed"
        ),
    )
    op.create_index(
        "ix_validated_decision_user", "validated_decision", ["user_id"]
    )

    # -- validated_decision_outcome — append-only, exactly-once grade ------------
    op.create_table(
        "validated_decision_outcome",
        sa.Column("outcome_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "validated_decision_id",
            sa.Integer(),
            sa.ForeignKey("validated_decision.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("evaluation_window_id", sa.Text(), nullable=False),
        sa.Column("benchmark_version", sa.Text(), nullable=False),
        sa.Column("exposure_mapping_version", sa.Text(), nullable=False),
        sa.Column("calculator_version", sa.Text(), nullable=False),
        sa.Column("linking_algorithm_version", sa.Text(), nullable=True),
        # §2A(c) provenance columns (defect 6).
        sa.Column("outcome_kind", sa.Text(), nullable=True),
        sa.Column("post_mortem_category", sa.Text(), nullable=True),
        sa.Column("regime", sa.Text(), nullable=True),
        sa.Column(
            "shadow",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # DEFERRED — computed from the contribution_ledger in a later phase. Pinned
        # to NULL so a fabricated delta is impossible today (defect 5).
        sa.Column("vs_benchmark_delta", sa.Float(), nullable=True),
        sa.Column(
            "authored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("supersedes_outcome_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "vs_benchmark_delta IS NULL",
            name="ck_validated_decision_outcome_delta_deferred",
        ),
        # Idempotency: an identical retry (same 5 keys) is a no-op (spec §2A(c)).
        sa.UniqueConstraint(
            "validated_decision_id",
            "evaluation_window_id",
            "benchmark_version",
            "exposure_mapping_version",
            "calculator_version",
            name="uq_validated_decision_outcome_idem",
        ),
        # Exactly one successor per superseded outcome — the chain cannot fork.
        sa.UniqueConstraint(
            "supersedes_outcome_id", name="uq_validated_decision_outcome_supersedes"
        ),
        # A supersession may not cross decisions (same validated_decision_id).
        sa.ForeignKeyConstraint(
            ["supersedes_outcome_id", "validated_decision_id"],
            [
                "validated_decision_outcome.outcome_id",
                "validated_decision_outcome.validated_decision_id",
            ],
            name="fk_validated_decision_outcome_supersedes_same_decision",
        ),
        # Composite-unique target so the head's composite FK can bind
        # (validated_decision_id, current_outcome_id) — the DB then refuses a head
        # pointed at a DIFFERENT decision's outcome (mirrors integrity defect 4).
        sa.UniqueConstraint(
            "validated_decision_id",
            "outcome_id",
            name="uq_validated_decision_outcome_decision_id",
        ),
    )
    op.create_index(
        "ix_validated_decision_outcome_decision",
        "validated_decision_outcome",
        ["validated_decision_id"],
    )
    # One ROOT per decision — a partial unique index forbids a second disconnected
    # supersedes-NULL chain root (defect 4a). SQLite supports partial indexes.
    op.create_index(
        "uq_vdo_root",
        "validated_decision_outcome",
        ["validated_decision_id"],
        unique=True,
        sqlite_where=sa.text("supersedes_outcome_id IS NULL"),
    )

    # -- validated_decision_outcome_head — the single current head, CAS-advanced -
    op.create_table(
        "validated_decision_outcome_head",
        sa.Column(
            "validated_decision_id",
            sa.Integer(),
            sa.ForeignKey("validated_decision.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("current_outcome_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        # Composite FK — the head may ONLY point at an outcome of the SAME decision.
        sa.ForeignKeyConstraint(
            ["validated_decision_id", "current_outcome_id"],
            [
                "validated_decision_outcome.validated_decision_id",
                "validated_decision_outcome.outcome_id",
            ],
            name="fk_validated_decision_outcome_head_same_decision",
        ),
    )

    _create_immutability_triggers()


def downgrade() -> None:
    _drop_immutability_triggers()
    op.drop_table("validated_decision_outcome_head")
    op.drop_index("uq_vdo_root", table_name="validated_decision_outcome")
    op.drop_index(
        "ix_validated_decision_outcome_decision",
        table_name="validated_decision_outcome",
    )
    op.drop_table("validated_decision_outcome")
    op.drop_index("ix_validated_decision_user", table_name="validated_decision")
    op.drop_table("validated_decision")
    op.drop_index("ix_observed_decision_subject", table_name="observed_decision")
    op.drop_index("ix_observed_decision_user", table_name="observed_decision")
    op.drop_table("observed_decision")
