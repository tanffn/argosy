"""life_events cashflow-shape extension — Spec D commit #1.

Revision ID: 0054_life_events_cashflow_shape
Revises: 0053_predictions_provenance
Create Date: 2026-05-30

Spec D (/life-events cashflow redesign) commit #1.  Spec text says
"migration 0049" but Spec B (state-observer) already claimed that
revision number in this sprint — see migrations 0048 / 0049 / 0050 /
0051 / 0052 / 0053 which landed between the spec being written and
this commit.  Semantic content is identical to the spec's §1.1 / §1.5 /
Appendix A.

Three operations in this migration:

1. **Extend ``life_events``** with the ``delta_kind`` discriminator + the
   per-shape amount / date columns the new cashflow model uses (spec
   §1.1 + Appendix A).  ``delta_kind`` is a CHECK-enforced enum over
   five values:

     * ``one_shot``                  — single spike on ``one_shot_*``
     * ``recurring_every_n_years``   — periodic spike, anchored on
                                       ``recurring_*`` fields
     * ``phase_change_start``        — step function starting at
                                       ``phase_start_date`` (open-ended)
     * ``phase_change_end``          — step function bounded by
                                       ``phase_start_date`` and
                                       ``phase_end_date``
     * ``none``                      — no cashflow effect (display-only)

   The column lands with ``server_default='none'`` so the SQLite batch
   rebuild populates every existing row (even those that the data
   conversion below DOESN'T reach via the documented decision table —
   the default makes the "anything else falls through to none"
   contract enforced at the DDL layer, not just by the Python loop).

   The per-shape value columns (``monthly_delta_usd`` /
   ``one_shot_amount_usd`` / ``recurring_amount_usd`` /
   ``recurring_period_years`` / ``phase_start_date`` /
   ``phase_end_date``) all land nullable — the shape-consistency
   CHECK is the writer's responsibility (Pydantic discriminator in
   commit #4); at the DB level we only enforce the simple "period > 0
   when present" guarantee, matching the original migration 0042
   pattern of ``recurring_years > 0``.

   ``fx_at_event`` captures the USD→NIS exchange rate at WRITE time
   per spec §1.4 / IMPORTANT #4 — the cashflow projection looks this
   up only when the row is created, so a future FX shift doesn't
   silently re-price the user's wedding gift estimate.  Nullable
   because legacy rows don't have it; new writes set it from the
   service layer (commit #4).

2. **Create ``life_events_migration_log``** — one audit row per
   life_events row this migration touches.  The table is permanent
   (not dropped after the migration completes) so the user can audit
   conversions via a future ``/life-events?show=migration_log`` query
   and so a rollback can reconstruct the original shape.  Carries:

     * ``original_life_event_id`` — FK to ``life_events.id`` with
       ``ON DELETE CASCADE`` so deleting an event also tombstones its
       conversion log entry.
     * ``original_kind`` / ``original_amount_usd`` — captured so the
       reversal (downgrade) can re-derive the legacy row even after
       commit #4 starts editing the per-shape columns.
     * ``target_delta_kind`` — what the upgrade landed it as.
     * ``conversion_outcome`` — one of ``preserved`` /
       ``lossy_converted`` / ``flagged_review``.
     * ``user_decision`` NULL — filled by the conversion-assistant UI
       (commit #5).
     * ``notes`` — one-line human-readable explanation.

3. **Extend ``users``** with
   ``life_events_migration_acknowledged_at DATETIME NULL`` so the
   ``/life-events`` page can gate the "review your conversions" banner
   per spec §1.5 codex BLOCKER #2.  NULL = banner shown; non-NULL =
   user has clicked "I've reviewed all conversions".

Data conversion — runs INSIDE ``upgrade()`` after the DDL changes.
Decision table per the task spec (and aligned with §1.5 / §7.3 of the
design doc — codex BLOCKER #1: every row is CONVERTED, never DROPPED):

    Source row                                 Destination
    ----------------------------------------   ------------------------
    kind = 'retirement_milestone:*' with       delta_kind = 'none';
      target_date IS NOT NULL                   original target_date
                                                serialized into the
                                                description column;
                                                logged 'preserved'
    kind = 'expense_event:college'             delta_kind = 'none';
                                                logged 'flagged_review'
                                                so the conversion
                                                assistant UI offers an
                                                upgrade path
    kind = '*:other_asset_acquired' with       delta_kind = 'one_shot';
      amount_usd IS NOT NULL                    one_shot_amount_usd =
                                                amount_usd; logged
                                                'preserved'
    anything else                              delta_kind = 'none';
                                                logged 'lossy_converted'

The fall-through (anything-else → none + lossy_converted) is EXPLICIT
— a row whose ``kind`` doesn't match any of the three pinned cases
still gets a migration_log entry, so the user-facing banner correctly
surfaces "N rows were converted with partial information loss".  No
silent drops.

SQLite limitations
==================
``life_events`` is rebuilt via ``op.batch_alter_table`` because SQLite
cannot ALTER TABLE ADD CHECK constraints in place.  Same pattern as
migrations 0047 / 0049 / 0051 / 0053.

Downgrade
=========
Symmetric preflight per the 0049 pattern — refuse to downgrade if any
``life_events_migration_log`` row carries a non-NULL ``user_decision``
(i.e. the user has acted on the conversion assistant and the
conversion has UX history we'd lose).  Operator remediation: review
the log table contents, then DELETE / set the ``user_decision`` to
NULL if you truly want to revert.

After the preflight passes the downgrade DROPs:
  * the new columns on ``life_events``
  * the ``life_events_migration_log`` table
  * the ``life_events_migration_acknowledged_at`` column on ``users``

The downgrade does NOT attempt to reconstitute the legacy schema's
``target_date`` / ``amount_usd`` / ``recurring_years`` fields beyond
what is already on the row (this commit doesn't drop those columns —
that's deferred to a later sprint commit once all consumers have
migrated to the per-shape columns).  Effectively the downgrade reverts
the EXTENSION, not the data conversion.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0054_life_events_cashflow_shape"
down_revision: str | None = "0053_predictions_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Five-value enum for the new delta_kind discriminator.  Order matches
# spec §1.1 (Appendix A).  ``none`` lands last because it's the default
# and the "anything-else" sink.
_VALID_DELTA_KINDS: tuple[str, ...] = (
    "one_shot",
    "recurring_every_n_years",
    "phase_change_start",
    "phase_change_end",
    "none",
)


def _delta_kinds_sql() -> str:
    return ", ".join(repr(k) for k in _VALID_DELTA_KINDS)


# Conversion-outcome enum for the migration_log table.
_VALID_CONVERSION_OUTCOMES: tuple[str, ...] = (
    "preserved",
    "lossy_converted",
    "flagged_review",
)


def _conversion_outcomes_sql() -> str:
    return ", ".join(repr(o) for o in _VALID_CONVERSION_OUTCOMES)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Extend ``life_events`` with delta_kind + per-shape columns.
    # ------------------------------------------------------------------
    #
    # SQLite cannot ALTER TABLE ADD CHECK constraints in place — the
    # batch helper rebuilds the table with the new columns + CHECKs
    # applied.  Same pattern as migrations 0047 / 0049 / 0051 / 0053.
    #
    # delta_kind lands with server_default='none' so existing rows pick
    # up the safe default during the SQLite copy-rename step.  The
    # Python data-conversion below then UPDATEs the rows that have a
    # documented destination shape.
    with op.batch_alter_table("life_events") as batch:
        batch.add_column(
            sa.Column(
                "delta_kind",
                sa.Text,
                nullable=False,
                server_default=sa.text("'none'"),
            )
        )
        batch.add_column(
            sa.Column("monthly_delta_usd", sa.Float, nullable=True)
        )
        batch.add_column(
            sa.Column("one_shot_amount_usd", sa.Float, nullable=True)
        )
        batch.add_column(
            sa.Column("recurring_amount_usd", sa.Float, nullable=True)
        )
        batch.add_column(
            sa.Column("recurring_period_years", sa.Integer, nullable=True)
        )
        batch.add_column(
            sa.Column("phase_start_date", sa.Date, nullable=True)
        )
        batch.add_column(
            sa.Column("phase_end_date", sa.Date, nullable=True)
        )
        # Locked FX at write time — spec §1.4 IMPORTANT #4.
        batch.add_column(
            sa.Column("fx_at_event", sa.Float, nullable=True)
        )
        batch.create_check_constraint(
            "ck_life_events_delta_kind",
            f"delta_kind IN ({_delta_kinds_sql()})",
        )
        batch.create_check_constraint(
            "ck_life_events_recurring_period_positive",
            "recurring_period_years IS NULL "
            "OR recurring_period_years > 0",
        )

    # ------------------------------------------------------------------
    # 2. Create ``life_events_migration_log``.
    # ------------------------------------------------------------------
    op.create_table(
        "life_events_migration_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "original_life_event_id",
            sa.Integer,
            sa.ForeignKey("life_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("original_kind", sa.Text, nullable=False),
        sa.Column("original_amount_usd", sa.Float, nullable=True),
        sa.Column("target_delta_kind", sa.Text, nullable=False),
        sa.Column("conversion_outcome", sa.Text, nullable=False),
        sa.Column("user_decision", sa.Text, nullable=True),
        sa.Column("notes", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            f"target_delta_kind IN ({_delta_kinds_sql()})",
            name="ck_life_events_migration_log_target_delta_kind",
        ),
        sa.CheckConstraint(
            "conversion_outcome IN ("
            + _conversion_outcomes_sql()
            + ")",
            name="ck_life_events_migration_log_conversion_outcome",
        ),
        # Idempotency floor — codex IMPORTANT (Spec D commit #1 review):
        # without a uniqueness constraint on the source-event reference,
        # a re-run of the upgrade (e.g. after a manual sqlite tweak that
        # bypassed alembic's version tracking) would double-log every
        # row.  Alembic SHOULD never re-run a migration on a clean cycle
        # (each downgrade drops this table outright before re-upgrade),
        # but the UNIQUE constraint pins the invariant at the DB layer
        # so operator footguns can't silently inflate the log.
        sa.UniqueConstraint(
            "original_life_event_id",
            name="uq_life_events_migration_log_event",
        ),
    )
    op.create_index(
        "ix_life_events_migration_log_event",
        "life_events_migration_log",
        ["original_life_event_id"],
    )

    # ------------------------------------------------------------------
    # 3. Extend ``users`` with the banner-acknowledgement timestamp.
    # ------------------------------------------------------------------
    #
    # Plain ADD COLUMN — no CHECK / no constraint change, so the batch
    # helper isn't strictly required.  We still use it for consistency
    # with the other two ops and to be defensive about SQLite quirks.
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "life_events_migration_acknowledged_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )

    # ------------------------------------------------------------------
    # 4. Data conversion — walk every existing life_events row and
    #    apply the documented decision table.
    # ------------------------------------------------------------------
    #
    # Per the task spec (and §1.5 / §7.3 of the design doc): every
    # source row gets a migration_log entry; no row is ever dropped.
    # The destination column population is by raw SQL UPDATE because
    # the ORM models in argosy/state/models.py are extended in the same
    # commit but aren't necessarily loadable from within an alembic
    # upgrade (env.py imports Base but doesn't instantiate sessions).
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, kind, target_date, amount_usd, "
            "       description "
            "FROM life_events"
        )
    ).fetchall()

    for row in rows:
        event_id = row[0]
        kind = row[1] or ""
        target_date = row[2]
        amount_usd = row[3]
        description = row[4] or ""

        # Decision table — order matters; the first match wins.  The
        # last branch (else) is the explicit fall-through.
        if (
            kind.startswith("retirement_milestone")
            and target_date is not None
        ):
            new_delta_kind = "none"
            outcome = "preserved"
            note = (
                "Originally a retirement milestone at "
                f"{target_date}; converted to delta_kind=none under "
                "the new cashflow-shape model.  The target_date is "
                "preserved in the description field for audit."
            )
            # Append a human-readable marker to the description so the
            # user still sees the historical intent on the timeline /
            # recorded-events list, per codex BLOCKER #1.
            marker = (
                f"Originally a retirement milestone at {target_date}"
            )
            new_description = (
                f"{description}\n\n{marker}".strip()
                if description
                else marker
            )
            conn.execute(
                sa.text(
                    "UPDATE life_events SET "
                    "  delta_kind = :dk, description = :desc "
                    "WHERE id = :eid"
                ),
                {
                    "dk": new_delta_kind,
                    "desc": new_description,
                    "eid": event_id,
                },
            )
        elif kind == "expense_event:college":
            new_delta_kind = "none"
            outcome = "flagged_review"
            note = (
                "Legacy expense_event:college row — converted to "
                "delta_kind=none; user will be prompted to re-classify "
                "(one_shot per year vs phase_change_end across all "
                "years) via the /life-events conversion-assistant "
                "modal in commit #5."
            )
            conn.execute(
                sa.text(
                    "UPDATE life_events SET delta_kind = :dk "
                    "WHERE id = :eid"
                ),
                {"dk": new_delta_kind, "eid": event_id},
            )
        elif (
            "other_asset_acquired" in kind
            and amount_usd is not None
        ):
            new_delta_kind = "one_shot"
            outcome = "preserved"
            note = (
                "Legacy other_asset_acquired row with amount — "
                "converted to delta_kind=one_shot; "
                "one_shot_amount_usd populated from amount_usd."
            )
            conn.execute(
                sa.text(
                    "UPDATE life_events SET "
                    "  delta_kind = :dk, "
                    "  one_shot_amount_usd = :amt "
                    "WHERE id = :eid"
                ),
                {
                    "dk": new_delta_kind,
                    "amt": float(amount_usd),
                    "eid": event_id,
                },
            )
        else:
            # Fall-through: anything else converts to delta_kind=none
            # and is logged as lossy_converted with a one-line
            # explanation of what was lost.  This includes:
            #   - career_event:* without cashflow detail
            #   - family_event:* without cashflow detail
            #   - recurring_expense:* (legacy ``recurring_years``
            #     semantics couldn't be cleanly mapped without the
            #     anchor-date field, which this commit doesn't add)
            #   - any retirement_milestone:* without a target_date
            #   - any kind we don't recognize
            new_delta_kind = "none"
            outcome = "lossy_converted"
            note = (
                f"Legacy row of kind={kind!r} did not match any of "
                "the three pinned conversion cases "
                "(retirement_milestone with target_date / "
                "expense_event:college / "
                "*:other_asset_acquired with amount).  Converted to "
                "delta_kind=none; original kind preserved in "
                "life_events.kind for audit.  User may want to add "
                "the financial impact via the new form."
            )
            # delta_kind already defaulted to 'none' by the
            # server_default; no UPDATE needed.  We still log.

        conn.execute(
            sa.text(
                "INSERT INTO life_events_migration_log "
                "  (original_life_event_id, original_kind, "
                "   original_amount_usd, target_delta_kind, "
                "   conversion_outcome, notes) "
                "VALUES (:eid, :kind, :amt, :dk, :outcome, :notes)"
            ),
            {
                "eid": event_id,
                "kind": kind,
                "amt": float(amount_usd) if amount_usd is not None else None,
                "dk": new_delta_kind,
                "outcome": outcome,
                "notes": note,
            },
        )

    # ------------------------------------------------------------------
    # 5. Auto-acknowledge users with no migration_log rows.
    # ------------------------------------------------------------------
    #
    # Per spec §1.5: "a user with no migration_log rows (fresh DB,
    # never had legacy life events) has the column auto-set to the
    # migration's run timestamp so the banner never appears for them."
    # We set the timestamp here so the /life-events page logic in
    # commit #5 can treat NULL as "user has unacknowledged conversion
    # log entries".
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        sa.text(
            "UPDATE users SET "
            "  life_events_migration_acknowledged_at = :ts "
            "WHERE id NOT IN ( "
            "  SELECT DISTINCT le.user_id "
            "  FROM life_events_migration_log lem "
            "  JOIN life_events le ON le.id = lem.original_life_event_id "
            ")"
        ),
        {"ts": now_iso},
    )


def _preflight_downgrade() -> None:
    """Refuse to downgrade if the conversion assistant has UX history.

    Symmetric mirror of the 0049 pattern: if any row in
    ``life_events_migration_log`` carries a non-NULL ``user_decision``,
    that means the user has clicked through the conversion-assistant
    modal and acted on the conversion.  Dropping the log table would
    lose that history.  Better to halt and let the operator decide
    than to silently drop UX-relevant data.

    Operator remediation: either DELETE the rows whose ``user_decision``
    is populated, or UPDATE those rows to NULL.  Both are loud
    operations and document the intent.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("life_events_migration_log"):
        # Fresh DB / never upgraded; nothing to preflight.
        return

    rows = bind.execute(
        sa.text(
            "SELECT id, original_life_event_id, user_decision "
            "FROM life_events_migration_log "
            "WHERE user_decision IS NOT NULL"
        )
    ).fetchall()
    if rows:
        ids = sorted(int(r[0]) for r in rows)
        raise RuntimeError(
            "Migration 0054 downgrade preflight failed: "
            "life_events_migration_log contains rows with non-NULL "
            f"user_decision (ids: {ids}).  Dropping the log table "
            "would lose the user's conversion-assistant history.  "
            "Remediate (DELETE the rows, or UPDATE user_decision to "
            "NULL) before retrying the downgrade.  See migration "
            "0054 downgrade docstring."
        )


def downgrade() -> None:
    # Preflight FIRST so any subsequent DDL doesn't half-execute on
    # SQLite (which runs DDL outside the migration transaction — same
    # property leveraged by migration 0049's downgrade).
    _preflight_downgrade()

    # 1. Drop the users column.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("life_events_migration_acknowledged_at")

    # 2. Drop the migration log table.
    op.drop_index(
        "ix_life_events_migration_log_event",
        table_name="life_events_migration_log",
    )
    op.drop_table("life_events_migration_log")

    # 3. Drop the new columns + CHECKs on life_events.
    with op.batch_alter_table("life_events") as batch:
        batch.drop_constraint(
            "ck_life_events_delta_kind", type_="check"
        )
        batch.drop_constraint(
            "ck_life_events_recurring_period_positive",
            type_="check",
        )
        batch.drop_column("fx_at_event")
        batch.drop_column("phase_end_date")
        batch.drop_column("phase_start_date")
        batch.drop_column("recurring_period_years")
        batch.drop_column("recurring_amount_usd")
        batch.drop_column("one_shot_amount_usd")
        batch.drop_column("monthly_delta_usd")
        batch.drop_column("delta_kind")
