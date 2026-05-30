"""replan_dispatch_log — observer→replan dispatch audit.

Revision ID: 0056_replan_dispatch_log
Revises: 0055_action_proposals_and_notifications
Create Date: 2026-05-30

Spec E (last-mile delivery) commit #4 — the observer→replan dispatcher's
audit table.  See ``docs/superpowers/specs/2026-05-29-last-mile-delivery-design.md``
§4 (observer→replan architecture), §4.2 (flag-kind → trigger-kind
mapping), §4.3 (atomic gates + cooldown), §4.4 (dispatch job), and
Appendix A.8 (DDL).

The dispatcher writes ONE row per maybe-dispatch decision so the admin
UI can audit "why did the system not replan when FX shifted again?"
Every gate evaluated by ``argosy/services/replan_dispatcher.py::
maybe_dispatch_replan`` produces a row regardless of outcome:

* ``fired`` — all gates clear; ``JobRegistry.fire_now`` was called;
  ``job_run_id`` is the audit row id from ``job_runs``.
* ``dry_run_logged`` — flag was mapped + severity below the per-trigger
  threshold; the row exists so the operator can audit warning-band
  observer fires that DID map to a replan kind but were filtered out
  by the severity gate (spec §4.2 — "critical only fires automatically;
  warning is dry-run-logged").
* ``skipped_cooldown`` — same (user, trigger_kind) fired within the
  per-kind cooldown window (default 72h; spec §4.3 Gate 1).
* ``skipped_global_cap`` — user already has 4 ``fired`` rows in the
  last 72h regardless of trigger_kind (spec §4.3 Gate 2).
* ``skipped_severity`` — flag's kind is not in the mapping table OR
  the severity didn't meet the trigger's minimum (spec §4.3 Gate 3).
* ``error`` — ``JobRegistry.fire_now`` raised; the row was inserted as
  ``fired`` first but the job_run_id stayed NULL and the status was
  flipped to ``error`` afterwards.  Idempotency-on-retry path (codex
  IMPORTANT: "if JobRegistry.fire_now raises, the log row should
  reflect that") — see ``maybe_dispatch_replan`` for the flip logic.

CHECK enum on ``trigger_kind`` mirrors
``argosy/services/retirement/replan_triggers.py::ALL_DISPATCH_TRIGGER_KINDS``
— the seven classical replan_triggers.TriggerKind values plus two
synthetic kinds for the observer→replan dispatcher's audit log
(``observer_emergent_critical`` / ``observer_emergent_warning_dry_run``).
Keeping these in the enum lets the dispatcher record dispatch decisions
on observer flags whose underlying kind maps to "the dispatcher saw
something" but not to a classical trigger.

Cooldown query hot-path: ``WHERE user_id = ? AND trigger_kind = ?
ORDER BY dispatched_at DESC LIMIT 1`` (per-trigger-kind cooldown gate)
AND ``WHERE user_id = ? AND status = 'fired' AND dispatched_at >
now - 72h`` (global cap gate).  The composite index
``ix_replan_dispatch_log_user_dispatched`` covers both — the cooldown
gate's per-trigger-kind subquery walks the index in dispatched_at DESC
order until the first row of the matching trigger_kind is found; the
global cap gate scans the same index slice for status='fired' rows
within the 72h window.

FKs:
  * ``user_id`` -> users.id ON DELETE CASCADE.
  * ``source_flag_id`` -> monitor_flags.id ON DELETE SET NULL.  Losing
    the source flag (housekeeping sweep) must NOT cascade away the
    dispatch audit row.
  * ``job_run_id`` -> job_runs.id ON DELETE SET NULL.  Losing the
    job_run (retention loop cleanup beyond N days) must NOT cascade
    away the dispatch audit row — the dispatch log is a longer-lived
    audit surface than job_runs.

SQLite version requirement: partial-index WHERE clauses + CHECK
constraints — already exercised in migrations 0040 / 0043 / 0047 /
0048 / 0049 / 0050 / 0055.

Downgrade
=========
Drops ``replan_dispatch_log`` + its index.  No data preservation —
the table is fresh in this migration.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_replan_dispatch_log"
down_revision: str | None = "0055_action_proposals_and_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Spec §4.2 + ``replan_triggers.ALL_DISPATCH_TRIGGER_KINDS``.  Mirrored
# here verbatim (NOT imported) so the migration file is self-contained
# and doesn't take a runtime dependency on the application package —
# matches the pattern in migration 0049 / 0055 where CHECK enums are
# inlined.  When a new trigger_kind lands, BOTH this tuple AND
# replan_triggers.ALL_DISPATCH_TRIGGER_KINDS must be extended (a check
# in ``tests/test_replan_dispatcher.py`` asserts the two stay aligned).
_VALID_TRIGGER_KINDS: tuple[str, ...] = (
    "market_drawdown_15pct",
    "job_change",
    "tax_law_change",
    "health_event",
    "fx_shock_10pct",
    "life_event",
    "user_request",
    "observer_emergent_critical",
    "observer_emergent_warning_dry_run",
)


# Spec §4.3 — six outcome statuses + error.  ``fired`` writes
# job_run_id; ``error`` may also have job_run_id set if fire_now
# committed the job_runs row before raising downstream.
_VALID_OUTCOMES: tuple[str, ...] = (
    "fired",
    "dry_run_logged",
    "skipped_cooldown",
    "skipped_global_cap",
    "skipped_severity",
    "error",
)


# Severity bands — shared across action_proposals + monitor_flags +
# notification tables (info / warning / critical).
_VALID_SEVERITIES: tuple[str, ...] = ("info", "warning", "critical")


def _quoted_csv(values: Sequence[str]) -> str:
    return ", ".join(repr(v) for v in values)


def upgrade() -> None:
    op.create_table(
        "replan_dispatch_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # FK to the observer flag that fired the dispatcher.  ON DELETE
        # SET NULL — losing the source flag (housekeeping sweep that
        # drops acknowledged flags) must NOT cascade away the dispatch
        # audit row.  NULLABLE because the dispatcher may also be fired
        # manually (e.g. via /admin/replan-now in a future commit) with
        # no underlying flag.
        sa.Column(
            "source_flag_id",
            sa.Integer,
            sa.ForeignKey("monitor_flags.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # ``trigger_kind`` — the replan_triggers.TriggerKind (or one of
        # the two synthetic ``observer_emergent_*`` kinds) that the
        # dispatcher resolved from the flag's kind.  CHECK enforced.
        sa.Column("trigger_kind", sa.Text, nullable=False),
        # ``severity`` — the underlying flag's severity at dispatch
        # time.  Persisted (NOT looked up via source_flag_id) so the
        # audit row survives source-flag deletion.
        sa.Column("severity", sa.Text, nullable=False),
        # ``status`` — the gate outcome (see module docstring for
        # semantics).  CHECK enforced.
        sa.Column("status", sa.Text, nullable=False),
        # ``job_run_id`` — populated when status='fired' AND
        # JobRegistry.fire_now returned successfully.  NULLABLE because
        # (a) skipped outcomes don't fire and (b) the error-on-fire
        # path leaves it NULL even though status='fired' (then flipped
        # to 'error').  ON DELETE SET NULL so the job_runs retention
        # loop's cleanup doesn't cascade away the dispatch row.
        sa.Column(
            "job_run_id",
            sa.Integer,
            sa.ForeignKey("job_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # ``dispatched_at`` — the instant the dispatcher made the
        # decision.  Used by the cooldown query
        # (last_fired_at = MAX(dispatched_at) WHERE status='fired').
        sa.Column(
            "dispatched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # ``notes`` — free-form audit blob.  The dispatcher populates
        # this with the cooldown remaining minutes / global cap count /
        # severity floor / underlying error message for skipped and
        # error rows; ``fired`` rows leave it NULL.
        sa.Column("notes", sa.Text, nullable=True),
        # ----- CHECK constraints (declared inline so SQLite
        # materialises them into the CREATE TABLE statement). -----
        sa.CheckConstraint(
            "trigger_kind IN (" + _quoted_csv(_VALID_TRIGGER_KINDS) + ")",
            name="ck_replan_dispatch_log_trigger_kind",
        ),
        sa.CheckConstraint(
            "severity IN (" + _quoted_csv(_VALID_SEVERITIES) + ")",
            name="ck_replan_dispatch_log_severity",
        ),
        sa.CheckConstraint(
            "status IN (" + _quoted_csv(_VALID_OUTCOMES) + ")",
            name="ck_replan_dispatch_log_status",
        ),
    )

    # Cooldown + global-cap hot-path: per-user, dispatched_at DESC.
    # The cooldown gate scans the index slice for the user, looking
    # for the most-recent ``fired`` row with the same trigger_kind;
    # the global-cap gate scans the same slice counting ``fired`` rows
    # within the 72h window.  One composite index covers both queries.
    op.create_index(
        "ix_replan_dispatch_log_user_dispatched",
        "replan_dispatch_log",
        ["user_id", sa.text("dispatched_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_replan_dispatch_log_user_dispatched",
        table_name="replan_dispatch_log",
    )
    op.drop_table("replan_dispatch_log")
