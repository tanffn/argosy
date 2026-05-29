"""predictions.provenance_weights_applied flag — Spec C commit #6.

Revision ID: 0053_predictions_provenance
Revises: 0052_source_reliability_view
Create Date: 2026-05-30

Spec C (predictions ledger) commit #6 — adds the
``provenance_weights_applied`` boolean column to ``predictions`` so the
anti-feedback-loop contract from spec §6.6 (Codex IMPORTANT 3 fix) can
fire at every consumer entry point.

Why this exists
===============

A signal can flow through multiple Argosy consumers before reaching the
user. The chain looks like:

    Discord message
      → news_signal_analyst (reads predictions ledger; downweights
        discord input by reliability)
      → per_position_thesis (reads predictions ledger; downweights
        news_signal_analyst input by reliability)
      → plan_synthesizer (reads predictions ledger; downweights
        per_position_thesis input by reliability)
      → state_observer (re-reads its own prior monitor_flag history,
        potentially downweighting again)

Without an idempotency stamp, each consumer would re-apply
``get_weight_for_source`` against the same upstream source, compounding
the attenuation across the chain (0.5 × 0.5 × 0.5 = 0.125). The
``cumulative_attenuation`` floor of 0.10 in
``argosy/services/predictions/reliability.py`` is the SAFETY NET; the
stamping discipline is the primary mechanism that prevents the
death-spiral.

Behaviour
=========

* Default ``0`` (FALSE) — every newly-written prediction starts
  un-stamped.
* Consumers that apply a reliability weight write the resulting
  prediction-derivative (a new ``predictions`` row, or an in-flight
  ``AnalyzedSignal`` / ``PositionThesis`` etc.) with the flag set to
  ``1`` (TRUE).
* Downstream consumers consult the flag via
  ``argosy/services/predictions/reliability.py::get_weight_for_source``
  (commit #6 modification) and SKIP re-applying the weight when the
  flag is set, returning ``1.0`` instead.

Why a column on ``predictions`` and not a separate table
========================================================

The flag is a per-row attribute of the prediction itself, not a
relationship. A consumer that emits a derivative prediction stamps
``provenance_weights_applied = 1`` on the new row at INSERT time;
downstream queries against the same row read the stamp without a join.
Putting it on the prediction row keeps the hot-path query (the
``source_reliability`` view + ``get_weight_for_source``) free of an
extra join.

In-flight derivatives (an ``AnalyzedSignal`` Pydantic instance carrying
a downstream-weighted signal that has not yet written to the ledger)
carry the flag in-memory; they only persist to a ``predictions`` row at
the next writer call site, at which point the column captures the same
boolean.

Backfill semantics
==================

Existing rows (predictions written before this commit) default to
``0``. This is correct: the old code never applied any provenance
weighting, so every pre-commit row was effectively un-weighted at the
source-of-record. New consumer code post-commit treats them as
"weights may be applied freshly" — which matches the historical
behaviour.

SQLite limitations
==================

The CHECK constraint ``provenance_weights_applied IN (0, 1)`` is added
inside an ``op.batch_alter_table`` block — SQLite cannot ALTER TABLE
ADD CONSTRAINT in place, so the batch helper rebuilds the table with
the new column + CHECK applied. Same pattern as migrations 0047 / 0049
/ 0051's batch helpers.

**View dependency.** Migration 0052 creates a ``source_reliability``
VIEW that references the ``predictions`` table by name. SQLite's
batch_alter_table rebuilds the table via a temp-rename dance
(``_alembic_tmp_predictions`` → ``predictions``); when the original
``predictions`` table is dropped during the rename the view becomes
dangling and the rename fails with ``error in view
source_reliability: no such table: main.predictions``. Mitigation:
we DROP the view before the batch, then RECREATE it after — using
the same SQL the 0052 migration ships. Per the 0052 module docstring
the view SQL is the canonical source-of-truth string; we re-import
it from the 0052 module rather than re-paste to keep the two
definitions in lockstep.

Index policy
============

No index added. The column is filtered at the WRITER side (every
consumer that emits a new prediction stamps it explicitly), not the
READER side — there is no "find all predictions with
provenance_weights_applied = 1" hot-path query. The 1-byte INTEGER
column adds negligible row width; SQLite's per-row overhead absorbs
it.
"""
from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0053_predictions_provenance"
down_revision: str | None = "0052_source_reliability_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _load_view_sql_from_0052() -> str:
    """Import ``_VIEW_SQL`` from the 0052 migration file by path.

    Migration files have leading-digit module names and aren't normally
    importable. The Alembic versions package is sibling to this file;
    we use :func:`importlib.util.spec_from_file_location` to load the
    0052 module directly and re-use its view-SQL string so the
    upgrade/downgrade halves of THIS migration stay byte-identical to
    what 0052 originally CREATEd.
    """
    here = Path(__file__).resolve().parent
    sibling = here / "0052_source_reliability_view.py"
    spec = importlib.util.spec_from_file_location(
        "argosy_alembic_0052_source_reliability_view", sibling
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(
            f"failed to load view-SQL from sibling migration {sibling}"
        )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._VIEW_SQL  # type: ignore[attr-defined]


def upgrade() -> None:
    # Drop the dependent view BEFORE the batch rebuild — see module
    # docstring "View dependency" note. We re-create it at the end of
    # the migration using the SAME SQL the 0052 migration shipped.
    op.execute("DROP VIEW IF EXISTS source_reliability")

    # SQLite cannot ALTER TABLE ADD CONSTRAINT in place; the batch
    # helper rebuilds the table with the new column + CHECK applied.
    # Same pattern as 0047 / 0049 / 0051.
    #
    # The column lands with server_default='0' so the rebuild's
    # INSERT-from-temp step populates existing rows with the correct
    # default. After the rebuild the column is NOT NULL — every
    # prediction row carries an explicit boolean.
    with op.batch_alter_table("predictions") as batch:
        batch.add_column(
            sa.Column(
                "provenance_weights_applied",
                sa.Integer,
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.create_check_constraint(
            "ck_predictions_provenance_weights_applied_bool",
            "provenance_weights_applied IN (0, 1)",
        )

    # Re-create the view with the SAME SQL string the 0052 migration
    # shipped. Loaded by path so a future rename / move of the 0052
    # module fails loudly rather than silently leaving a stale view
    # definition behind.
    op.execute(_load_view_sql_from_0052())


def downgrade() -> None:
    # Drop the view first so the batch rebuild can run cleanly.
    op.execute("DROP VIEW IF EXISTS source_reliability")

    with op.batch_alter_table("predictions") as batch:
        batch.drop_constraint(
            "ck_predictions_provenance_weights_applied_bool",
            type_="check",
        )
        batch.drop_column("provenance_weights_applied")

    # Re-create the view at the prior (0052) head so an upgrade-then-
    # downgrade cycle leaves the schema in the same shape it was after
    # 0052 applied.
    op.execute(_load_view_sql_from_0052())
