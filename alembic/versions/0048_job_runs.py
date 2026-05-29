"""job_runs: audit-history of every JobRegistry tick.

Revision ID: 0048_job_runs
Revises: 0047_expense_review_queue_extensions
Create Date: 2026-05-29

Spec A (jobs-registry) commit #1 — the audit-history table the
JobRegistry (commit #3a) writes to on every scheduled tick + every
manual `POST /api/jobs/{name}/run-now` invocation. One row per
execution; status walks through `running` → `ok` / `error` / `skipped`
/ `cancelled`.

Columns per spec §2.1:

* ``job_name`` — registry key (matches ``CadenceState.loop_name`` for
  cadence jobs; long-running jobs use their registry name).
* ``started_at`` / ``finished_at`` — DateTime(timezone=True). UTC at
  rest; UI renders in Asia/Jerusalem.
* ``status`` — CHECK enum: ``running`` / ``ok`` / ``error`` /
  ``skipped`` / ``cancelled``. A ``running`` row stays in that state
  until the registry's ``_close_job_run`` flips it; the cleanup pass
  (§2.1) reaps rows stuck > 24h.
* ``skip_reason`` — populated when ``status='skipped'``; UI collapses
  by default per codex IMPORTANT #4 on spec A. Examples:
  ``market_hours_guard``, ``cost_cap_reached``, ``cooldown_active``.
* ``error_message`` — populated when ``status='error'``. Truncated to
  4 KB at write time so a runaway exception trace doesn't bloat the
  row.
* ``manual_trigger`` — INTEGER 0/1 (SQLite-native bool). True when the
  run was kicked off via the `/api/jobs/{name}/run-now` route.
* ``triggered_by`` — free-form text for v1: ``"scheduler"`` for
  cron-driven ticks, ``"user"`` for manual triggers, or future
  upstream-job identifiers when one job fires another.
* ``output_summary`` — JSON-shaped TEXT (CHECK json_valid). The
  per-job summary the tick produces (e.g. ``news_daily`` writes
  ``{"signals_extracted": 47, "high_materiality": 3}``). Per codex
  NICE #4 the CHECK enforces JSON shape so corrupted writes fail at
  write-time, not at read-time in the UI.
* ``duration_ms`` — derived ``finished_at - started_at`` in ms; stored
  rather than computed so the retention loop (commit #9) can compact
  on it without a join.
* ``idempotency_key`` — UNIQUE per codex IMPORTANT #1 (retry safety).
  Pattern: ``<job_name>:<UTC-iso-of-scheduled-tick-or-trigger-time>``.
  Cron jobs key off the scheduled tick (not the actual start) so a
  retry after a transport timeout doesn't double-insert.
* ``created_at`` — DEFAULT CURRENT_TIMESTAMP; for the rare case where
  ``started_at`` is supplied by a backfilling caller, ``created_at``
  records the insert moment.

Indexes:

* ``ix_job_runs_job_started`` — covers the common
  ``WHERE job_name = ? ORDER BY started_at DESC LIMIT N`` query
  (admin UI per-job history).
* ``ix_job_runs_status_started`` — PARTIAL on
  ``status IN ('running','error')`` per codex NICE #2; serves the
  health dashboard's "what's wrong right now?" query without
  scanning the entire (mostly ``ok``) table.

SQLite ``json_valid`` requires SQLite 3.38+ (already an Argosy
requirement — see ``argosy/config.py`` settings doc). Partial-index
syntax is fine under SQLite (verified via PRAGMA index_list pattern
exercised in migration 0047).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_job_runs"
down_revision: str | None = "0047_expense_review_queue_extensions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VALID_STATUSES = ("running", "ok", "error", "skipped", "cancelled")


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_name", sa.String(64), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "finished_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("skip_reason", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "manual_trigger",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("triggered_by", sa.String(64), nullable=True),
        sa.Column("output_summary", sa.Text, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ("
            + ", ".join(repr(s) for s in _VALID_STATUSES)
            + ")",
            name="ck_job_runs_status",
        ),
        sa.CheckConstraint(
            "manual_trigger IN (0, 1)",
            name="ck_job_runs_manual_trigger_bool",
        ),
        sa.CheckConstraint(
            "output_summary IS NULL OR json_valid(output_summary)",
            name="ck_job_runs_output_summary_json",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_job_runs_duration_nonneg",
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_job_runs_idempotency"
        ),
    )

    op.create_index(
        "ix_job_runs_job_started",
        "job_runs",
        ["job_name", sa.text("started_at DESC")],
    )

    # Partial index on the "what's wrong right now?" query — codex NICE #2.
    # SQLite supports partial indexes via `WHERE` clause on CREATE INDEX.
    op.execute(
        "CREATE INDEX ix_job_runs_status_started "
        "ON job_runs (status, started_at DESC) "
        "WHERE status IN ('running', 'error')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_job_runs_status_started")
    op.drop_index("ix_job_runs_job_started", table_name="job_runs")
    op.drop_table("job_runs")
