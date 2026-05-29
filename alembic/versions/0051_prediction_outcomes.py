"""prediction_outcomes + evaluation_method_registry + FK on predictions.

Revision ID: 0051_prediction_outcomes
Revises: 0050_predictions
Create Date: 2026-05-29

Spec C (predictions ledger) commit #2 — schema for the outcome evaluator's
output rows + the method-version registry that makes the evaluator
replay-safe. See
``docs/superpowers/specs/2026-05-29-predictions-ledger-design.md``:

* §1.3 + Appendix A (``prediction_outcomes`` DDL).
* §3.4 + Appendix A (``evaluation_method_registry`` DDL — Codex BLOCKER 1
  fix; the registry replaces the original CHECK enum so new method
  versions land via INSERT, not a schema migration).
* §5 (the four v1 scoring methods seeded into the registry).

Three operations land in this migration:

1. **Create ``evaluation_method_registry``** — the method-version table.
   Seeded with four v1 methods per spec §5: ``target_stop``,
   ``fixed_lookahead_7d``, ``fixed_lookahead_30d``,
   ``multi_basket_weighted``. Adding ``fixed_lookahead_30d_v2`` later
   is one INSERT + one UPDATE — no schema migration.

   NOTE: the spec's Appendix A lists FIVE seed values, the fifth being
   ``unparseable``. That entry is INCLUDED here so the writer-side
   method selection (§3.1 — "if unparseable_reason set → method =
   unparseable, window = 0") has a registry row to reference. The
   prose ("seed it with the initial methods") would otherwise create
   a foot-gun where the writer references a method that doesn't exist
   in the registry.

2. **Create ``prediction_outcomes``** — one row per evaluated
   prediction (per evaluation_method). UNIQUE(prediction_id,
   evaluation_method) is the natural key + the replay safety net:
   re-running the same v1 method over the same prediction is a no-op
   (``INSERT ... ON CONFLICT DO NOTHING`` in the evaluator); a NEW
   method version creates a separate row, keeping the prior outcome
   intact for audit. The view in §4.1 picks the most-recently-
   evaluated active method per family so the aggregate never double-
   counts a single prediction across method versions.

   ``outcome_kind`` CHECK enforces the six values from §2.4 +
   Appendix A: ``hit_target`` / ``hit_stop`` / ``expired_neutral`` /
   ``expired_positive`` / ``expired_negative`` / ``unparseable``.

   FK ``prediction_id → predictions(id) ON DELETE CASCADE`` — when a
   prediction is deleted (e.g. via the retention compactor of §9.1
   archiving very old rows) its outcomes go with it. The user can
   never have an orphan outcome row pointing at a missing prediction.

3. **Add FK ``predictions.evaluation_method → evaluation_method_registry.method_name``**
   (Codex BLOCKER 1 — the column on ``predictions`` was created in
   migration 0050 WITHOUT the FK because the registry table did not
   yet exist; we wire the FK here using ``op.batch_alter_table`` per
   the SQLite "ALTER TABLE cannot add a FK in place" limitation.
   Same pattern as migration 0047). The NOT-NULL constraint on
   ``predictions.evaluation_method`` is preserved — every prediction
   row carries the method it was written for, and that method MUST
   exist in the registry.

   The FK does NOT carry ON DELETE behavior — deleting a registry row
   should be impossible while predictions reference it; the user-
   facing path for retiring a method is ``is_active = FALSE`` (not
   DELETE).

Indexes per Appendix A:

* ``ix_outcomes_pred_method`` UNIQUE on (prediction_id, evaluation_method).
* ``ix_outcomes_evaluated`` on (evaluated_at) — backfill / replay cursor.
* ``ix_outcomes_kind`` on (outcome_kind) — view aggregation.

SQLite limitations exercised:

* ``op.batch_alter_table`` for the predictions FK add — SQLite cannot
  ``ALTER TABLE ... ADD CONSTRAINT FOREIGN KEY``; the batch helper
  rebuilds the table with the new FK applied (same shape as 0047 /
  0049 used for CHECK relaxations).

* PRAGMA ``foreign_keys = ON`` must be set per-connection for SQLite
  to ENFORCE FKs at write time. The Argosy session-factory enables
  this in ``argosy/state/session.py``; the migration assumes it.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_prediction_outcomes"
down_revision: str | None = "0050_predictions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Six outcome_kind values per spec §2.4 + Appendix A. Enumerated explicitly
# (not LIKE-pattern) so a typo at write time surfaces as IntegrityError
# instead of landing as a silently-mislabelled row.
_VALID_OUTCOME_KINDS = (
    "hit_target",
    "hit_stop",
    "expired_neutral",
    "expired_positive",
    "expired_negative",
    "unparseable",
)


# Initial method seeds per spec §5 + Appendix A. The fifth value
# (``unparseable``) is added so the writer-side method selection (§3.1 —
# "if unparseable_reason set → method = unparseable") references a row
# that actually exists in the registry. Without this, writers handling an
# unparseable input would either need a special-case skip (violating the
# "store evaluation_method on the row" invariant of §1.2) or fail FK at
# insert time.
#
# Format: (method_name, family, version, description).
_INITIAL_METHODS: tuple[tuple[str, str, int, str], ...] = (
    (
        "target_stop",
        "target_stop",
        1,
        "Score against explicit target_price and stop_price; "
        "adverse-first determinism rule when both touch same bar "
        "(spec §5.1 + §5.3).",
    ),
    (
        "fixed_lookahead_7d",
        "fixed_lookahead",
        1,
        "Classify by signed return at end of 7 trading-day window "
        "with +/-10% hit thresholds and +/-1% neutral band (spec §5.2).",
    ),
    (
        "fixed_lookahead_30d",
        "fixed_lookahead",
        1,
        "Classify by signed return at end of 30 trading-day window "
        "with +/-10% hit thresholds and +/-1% neutral band (spec §5.2 "
        "+ §5.5).",
    ),
    (
        "multi_basket_weighted",
        "multi_basket",
        1,
        "Weighted-average return across basket constituents, "
        "renormalising surviving weights when constituents are "
        "delisted (spec §5.4).",
    ),
    (
        "unparseable",
        "unparseable",
        1,
        "Method-of-record for predictions the writer flagged as "
        "structurally unscoreable (no ticker, no actionable direction). "
        "Counts toward coverage; excluded from reliability stats "
        "(spec §1.2 + §3.1).",
    ),
)


def _outcome_kinds_sql() -> str:
    return ", ".join(repr(k) for k in _VALID_OUTCOME_KINDS)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. evaluation_method_registry — created BEFORE prediction_outcomes
    #    (which has a FK into it) AND BEFORE the predictions FK add (which
    #    points at it). Order matters: SQLite refuses FK targets that
    #    don't exist yet.
    # ------------------------------------------------------------------
    op.create_table(
        "evaluation_method_registry",
        sa.Column("method_name", sa.Text, primary_key=True),
        sa.Column(
            "family",
            sa.Text,
            nullable=False,
            comment=(
                "Method family for the source_reliability view's "
                "'one outcome per prediction per family' filter "
                "(spec §3.4 Codex BLOCKER 1)."
            ),
        ),
        sa.Column(
            "method_version",
            sa.Integer,
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "is_active",
            sa.Integer,  # SQLite-native bool: 0/1
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "superseded_by",
            sa.Text,
            sa.ForeignKey(
                "evaluation_method_registry.method_name",
                # Self-FK — a retired method points at its replacement.
                # No CASCADE: retirement is a logical UPDATE, not a
                # DELETE.
                name="fk_eval_method_registry_superseded_by",
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "is_active IN (0, 1)",
            name="ck_eval_method_registry_is_active_bool",
        ),
        sa.CheckConstraint(
            "method_version >= 1",
            name="ck_eval_method_registry_version_positive",
        ),
    )

    # Seed the four v1 methods + the ``unparseable`` method-of-record.
    # Bulk-insert the rows under the migration's transaction so a
    # downgrade-then-upgrade cycle re-establishes the canonical seeds.
    op.bulk_insert(
        sa.table(
            "evaluation_method_registry",
            sa.column("method_name", sa.Text),
            sa.column("family", sa.Text),
            sa.column("method_version", sa.Integer),
            sa.column("description", sa.Text),
            sa.column("is_active", sa.Integer),
        ),
        [
            {
                "method_name": name,
                "family": family,
                "method_version": version,
                "description": description,
                "is_active": 1,
            }
            for (name, family, version, description) in _INITIAL_METHODS
        ],
    )

    # ------------------------------------------------------------------
    # 2. prediction_outcomes
    # ------------------------------------------------------------------
    op.create_table(
        "prediction_outcomes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "prediction_id",
            sa.Integer,
            sa.ForeignKey(
                "predictions.id",
                ondelete="CASCADE",
                name="fk_prediction_outcomes_prediction_id",
            ),
            nullable=False,
        ),
        sa.Column("outcome_kind", sa.Text, nullable=False),
        # NUMERIC(7,4) per Appendix A — signed P/L ratio (e.g. 0.0712
        # = +7.12% favourable). NULL for unparseable + when price data
        # was missing entirely.
        sa.Column("pnl_pct", sa.Numeric(7, 4), nullable=True),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "evaluation_method",
            sa.Text,
            sa.ForeignKey(
                "evaluation_method_registry.method_name",
                name="fk_prediction_outcomes_evaluation_method",
            ),
            nullable=False,
        ),
        sa.Column("entry_price_used", sa.Numeric(12, 4), nullable=True),
        sa.Column("exit_price_used", sa.Numeric(12, 4), nullable=True),
        sa.Column("exit_trigger_date", sa.Date, nullable=True),
        # Bounded per §1.3 — first/last bar + trigger bar for windows
        # > 14d. Storage budget operationalized by the retention job in
        # §9.1.
        sa.Column("evidence_json", sa.Text, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.CheckConstraint(
            f"outcome_kind IN ({_outcome_kinds_sql()})",
            name="ck_prediction_outcomes_kind",
        ),
        # The natural key + replay safety net. (prediction_id, method)
        # is unique — re-running a method over the same prediction is a
        # no-op via ON CONFLICT DO NOTHING; a NEW method creates a fresh
        # row.
        sa.UniqueConstraint(
            "prediction_id",
            "evaluation_method",
            name="ix_outcomes_pred_method",
        ),
    )

    # Index on evaluated_at for backfill / replay cursors.
    op.create_index(
        "ix_outcomes_evaluated",
        "prediction_outcomes",
        ["evaluated_at"],
    )

    # Index on outcome_kind for the source_reliability view's hit-rate
    # aggregation.
    op.create_index(
        "ix_outcomes_kind",
        "prediction_outcomes",
        ["outcome_kind"],
    )

    # ------------------------------------------------------------------
    # 3. Add FK predictions.evaluation_method -> evaluation_method_registry.method_name
    #    SQLite cannot ADD a FK via plain ALTER; batch_alter_table
    #    rebuilds the table with the FK applied. Same pattern as 0047
    #    and 0049's CHECK relaxations.
    #
    #    The NOT-NULL constraint on evaluation_method is preserved by
    #    the batch helper (it carries forward the column's nullability).
    # ------------------------------------------------------------------
    with op.batch_alter_table("predictions") as batch:
        batch.create_foreign_key(
            "fk_predictions_evaluation_method",
            "evaluation_method_registry",
            ["evaluation_method"],
            ["method_name"],
        )


def downgrade() -> None:
    # Reverse order — drop the FK from predictions FIRST so the registry
    # table is no longer referenced, THEN drop prediction_outcomes (its
    # FKs reference both predictions and the registry), THEN drop the
    # registry.

    with op.batch_alter_table("predictions") as batch:
        batch.drop_constraint(
            "fk_predictions_evaluation_method",
            type_="foreignkey",
        )

    op.drop_index(
        "ix_outcomes_kind",
        table_name="prediction_outcomes",
    )
    op.drop_index(
        "ix_outcomes_evaluated",
        table_name="prediction_outcomes",
    )
    op.drop_table("prediction_outcomes")

    op.drop_table("evaluation_method_registry")
