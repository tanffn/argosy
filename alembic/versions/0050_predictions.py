"""predictions: unified per-source prediction ledger.

Revision ID: 0050_predictions
Revises: 0049_state_snapshots_and_monitor_flags
Create Date: 2026-05-29

Spec C (predictions-ledger) commit #1 — the central ``predictions``
table that every signal source (Discord alpha calls, news_signal_analyst
LLM verdicts, SEC Form 4 / 13F / TipRanks / CapitolTrades adapters,
internal per-position thesis, state_observer flags, plan_monitor flags,
manual user gut-calls) writes to via the writer-adapter contract in
commit #3. Once landed, every prediction becomes scoreable by the
single source-agnostic outcome evaluator (commit #4) and aggregatable
in the source_reliability view (commit #5).

See ``docs/superpowers/specs/2026-05-29-predictions-ledger-design.md``:
* §1.2 — column-by-column rationale.
* §1.4 — worked examples showing the schema accommodates free-text
  Discord, structured 13F, qualitative state_observer, and multi-ticker
  rotation baskets without per-source columns.
* §2.3 — ``event_at`` vs ``created_at`` distinction (codex IMPORTANT 2
  fix). Backfill writers MUST pass the real-world prediction moment as
  ``event_at``; ``created_at`` defaults to insert time and is audit-only.
* §3.1 — writer-side method selection: ``evaluation_due_at`` and
  ``evaluation_method`` are pre-computed at write time (codex BLOCKER 2
  fix). The evaluator's due-query keys off ``evaluation_due_at`` so the
  30d-cap rule from §5.5 triggers at 30d, not at raw timeframe_days.
* §3.4 — method registry (codex BLOCKER 1 fix): ``evaluation_method``
  is plain TEXT today and migration 0051 (sibling commit) adds the FK
  into ``evaluation_method_registry``. New method versions land via
  INSERT into the registry, not a schema migration.
* Appendix A — full DDL this file implements.

**Migration numbering note** — the spec text calls this "migration 0048".
Sprint A (jobs-registry) shipped first and claimed 0048 for ``job_runs``;
Spec B (state-observer) then took 0049 for ``state_snapshots`` + the
``monitor_flags`` CHECK relaxation. This is therefore migration 0050;
the semantic content matches the spec's §1.2 + Appendix A predictions
table unchanged.

**Columns** (full DDL in Appendix A; the conceptual mapping):

* ``id`` — PK, autoincrement.
* ``user_id`` — FK to ``users.id`` with CASCADE delete. Single-user
  today (Ariel) but multi-tenant ready per SDD §12.5.
* ``source`` — CHECK enum across 11 v1 values: ``discord``,
  ``news``, ``sec_form_4``, ``tipranks``, ``sec_13f``,
  ``capitoltrades``, ``internal_per_position_thesis``,
  ``internal_news_signal_analyst``, ``internal_state_observer``,
  ``internal_monitor_flags``, ``manual_user``. Extending the enum
  later is a CHECK-relaxation migration (same shape as 0049's
  ``monitor_flags.kind`` relaxation).
* ``source_ref`` — JSON-shaped TEXT; per-source caller-defined shape
  per §1.2 (Discord: ``{"channel_id":..., "message_id":...}``;
  13F: ``{"filing_id":..., "fund_id":...}``; etc.). NOT NULL because
  every row must trace back to its origin record for replay /
  unparseable diagnostics.
* ``ticker`` — NULLABLE. NULL for multi-ticker baskets (use
  ``multi_ticker_json``) or macro predictions (e.g. "Fed will cut").
* ``direction`` — CHECK enum: ``long`` / ``short`` / ``neutral`` /
  ``multi``. ``neutral`` covers HOLD verdicts and qualitative flags
  per Codex BLOCKER 3 fix in §2.4 — HOLDs ARE logged so selection
  bias can be measured.
* ``entry_price`` / ``target_price`` / ``stop_price`` — NUMERIC(12,4)
  NULL. Entry is filled by the writer at price-adapter snapshot time
  (§2.3 — taken at ``event_at``, NOT at ``created_at``). Target / stop
  only set when the source provided them.
* ``timeframe_days`` — NULLABLE INTEGER. Per-source default fallback
  in §1.2. CHECK enforces strictly positive when present.
* ``multi_ticker_json`` — JSON list of basket constituents
  ``[{ticker, direction, weight}]`` for ``direction='multi'`` rows.
  CHECK json_valid when present.
* ``entry_prices_json`` — JSON map ``{ticker: entry_price}`` for the
  multi-ticker basket case (§5.4). CHECK json_valid when present.
* ``message_id`` — TEXT, stable per-source dedup key. The writer
  contract (§2.2) computes
  ``v1|predictions|<source>|<source-stable-entity-id>`` and stores it
  here. Per spec note in §1.2 the column name carries Discord-era
  semantics ("a message id") but is generalized to "stable per-source
  key" for all sources. The partial-unique index
  ``ix_predictions_source_messageid`` enforces dedup at the DB layer.
* ``raw_text_ref`` — NULLABLE TEXT pointer (e.g. ``news_signals.id:423``
  or a filing URL). Per spec §1.2 / SDD codex BLOCKER carry-over: this
  is NEVER injected into LLM prompts — citation-display only.
* ``unparseable_reason`` — NULLABLE TEXT. Writer sets this when the
  source content has no actionable shape; outcome evaluator marks the
  row ``unparseable`` and excludes from reliability stats (but counts
  in coverage).
* ``event_at`` — DATETIME(timezone=True), **NOT NULL**. The real-world
  prediction time per §2.3 — Discord msg ts, filing date, news publish
  time, internal-agent emit time. The ENTRY-PRICE snapshot anchors at
  this moment; the EVALUATION WINDOW starts at this moment. Backfill
  writers diverge ``event_at`` (14 days old) from ``created_at`` (now).
* ``created_at`` — DATETIME(timezone=True) NOT NULL DEFAULT
  CURRENT_TIMESTAMP. Audit-only insertion timestamp. Scoring math
  NEVER touches this column.
* ``evaluation_due_at`` — DATETIME(timezone=True), NOT NULL. Codex
  BLOCKER 2 fix. Pre-computed at write time by the writer as
  ``event_at + chosen_window_days`` where ``chosen_window`` follows
  §3.1's method-selection rules. The evaluator's "what's due?" query
  reads this directly so the §5.5 30-day cap on long-horizon
  predictions fires at 30 days, not at the raw timeframe (e.g. 90d
  for 13F).
* ``evaluation_method`` — TEXT, NOT NULL. Codex BLOCKER 1 fix. Carries
  the chosen method name (``target_stop`` / ``fixed_lookahead_7d`` /
  ``fixed_lookahead_30d`` / ``multi_basket_weighted`` / ``unparseable``)
  per §3.1's writer-side selection. **No CHECK enum on this column** —
  per §3.4 the FK into ``evaluation_method_registry`` (sibling
  migration 0051) is the source of truth. The registry pattern lets
  new method versions (e.g. ``fixed_lookahead_30d_v2``) land via
  INSERT, NOT via schema migration. Migration 0051 attaches the FK
  using the batch-alter pattern; until then the column is plain TEXT
  and the writer adapter is responsible for emitting registered
  method names.
* ``archived`` — INTEGER NOT NULL DEFAULT 0 (SQLite-native bool).
  Codex IMPORTANT 4 fix. Set to 1 by the
  ``predictions-retention-compact`` job (§9.1, NOT in this commit)
  after 2 years + 90d-inactive. The partial index
  ``ix_predictions_due_at`` excludes archived rows so the evaluator's
  scan stays bounded.

**Indexes** (per Appendix A):

* ``ix_predictions_source_event`` on ``(source, event_at DESC)`` —
  serves per-source reliability rollups walking newest-first.
* ``ix_predictions_ticker_event`` on ``(ticker, event_at DESC)``
  partial ``WHERE ticker IS NOT NULL`` — per-ticker historical
  lookup avoiding the multi-ticker / macro rows.
* ``ix_predictions_source_messageid`` UNIQUE on ``(source,
  message_id)`` partial ``WHERE message_id IS NOT NULL`` —
  per-source dedup; NULL ``message_id`` rows fall outside the
  uniqueness scope so legacy / manual-source rows without a stable
  key don't collide.
* ``ix_predictions_due_at`` on ``(evaluation_due_at)`` partial
  ``WHERE archived = 0`` — the evaluator's "what's due to score?"
  hot-path. Bounded by archive flag so a multi-year-old ledger stays
  fast.
* ``ix_predictions_event_at`` on ``(event_at)`` — general
  time-ordered scan (backfill cursors, reliability windowed views).

**SQLite version requirement**: ``json_valid`` (used in the two JSON
CHECKs) needs SQLite >= 3.38 — already an Argosy baseline (see
``argosy/config.py``). Partial-index WHERE clauses are SQLite-supported
and exercised in migrations 0040 / 0043 / 0047 / 0048 / 0049.

**FK to evaluation_method_registry**: NOT added in this migration. The
spec's Appendix A DDL has ``evaluation_method TEXT NOT NULL REFERENCES
evaluation_method_registry(method)``; that referenced table is created
in sibling migration 0051. The chain order is 0050 (this migration)
→ 0051 (registry + outcomes + FK). Until 0051 lands, the column is
plain TEXT — writer code is the single source of truth for valid
method names. This avoids a chicken-and-egg between the two parallel
sub-agent commits.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_predictions"
down_revision: str | None = "0049_state_snapshots_and_monitor_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Spec §1.2 — 11 v1 source values. Extending the enum is a CHECK-
# relaxation migration (same shape as 0049's monitor_flags.kind).
_VALID_SOURCES = (
    "discord",
    "news",
    "sec_form_4",
    "tipranks",
    "sec_13f",
    "capitoltrades",
    "internal_per_position_thesis",
    "internal_news_signal_analyst",
    "internal_state_observer",
    "internal_monitor_flags",
    "manual_user",
)

# Spec §1.2 — four direction values. ``neutral`` covers HOLD verdicts
# and qualitative state-observer flags per Codex BLOCKER 3 fix (§2.4);
# ``multi`` rows always have ``ticker IS NULL`` and read the basket
# from ``multi_ticker_json``.
_VALID_DIRECTIONS = ("long", "short", "neutral", "multi")


def _quoted_csv(values: Sequence[str]) -> str:
    return ", ".join(repr(v) for v in values)


def upgrade() -> None:
    op.create_table(
        "predictions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("source_ref", sa.Text, nullable=False),
        sa.Column("ticker", sa.Text, nullable=True),
        sa.Column("direction", sa.Text, nullable=False),
        sa.Column("entry_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("target_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("stop_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("timeframe_days", sa.Integer, nullable=True),
        sa.Column("multi_ticker_json", sa.Text, nullable=True),
        sa.Column("entry_prices_json", sa.Text, nullable=True),
        sa.Column("message_id", sa.Text, nullable=True),
        sa.Column("raw_text_ref", sa.Text, nullable=True),
        sa.Column("unparseable_reason", sa.Text, nullable=True),
        # Spec §2.3 — event_at is the real-world prediction time;
        # entry-price snapshot + evaluation window anchor here. Distinct
        # from created_at on backfill (codex IMPORTANT 2 fix).
        sa.Column(
            "event_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # Spec §3.1 / codex BLOCKER 2 fix — pre-computed at write time
        # as event_at + chosen_window_days. Evaluator due-query reads
        # this directly so §5.5's 30d cap fires at 30d.
        sa.Column(
            "evaluation_due_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        # Spec §3.4 / codex BLOCKER 1 fix — plain TEXT today; sibling
        # migration 0051 attaches the FK into evaluation_method_registry.
        # No CHECK enum on this column by design (new method versions
        # land via INSERT into the registry, not a schema migration).
        sa.Column("evaluation_method", sa.Text, nullable=False),
        # Codex IMPORTANT 4 — retention compact flag. SQLite-native bool
        # via INTEGER 0/1; aligns with the manual_trigger pattern from
        # migration 0048 and avoids the BOOLEAN-vs-INTEGER drift in
        # introspection across dialects.
        sa.Column(
            "archived",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.CheckConstraint(
            "source IN (" + _quoted_csv(_VALID_SOURCES) + ")",
            name="ck_predictions_source",
        ),
        sa.CheckConstraint(
            "direction IN (" + _quoted_csv(_VALID_DIRECTIONS) + ")",
            name="ck_predictions_direction",
        ),
        sa.CheckConstraint(
            "timeframe_days IS NULL OR timeframe_days > 0",
            name="ck_predictions_timeframe_positive",
        ),
        sa.CheckConstraint(
            "archived IN (0, 1)",
            name="ck_predictions_archived_bool",
        ),
        sa.CheckConstraint(
            "multi_ticker_json IS NULL OR json_valid(multi_ticker_json)",
            name="ck_predictions_multi_ticker_json_valid",
        ),
        sa.CheckConstraint(
            "entry_prices_json IS NULL OR json_valid(entry_prices_json)",
            name="ck_predictions_entry_prices_json_valid",
        ),
    )

    # Per-source reliability rollups walk newest-first; DESC index
    # avoids a sort step. Same shape as 0048's ix_job_runs_job_started.
    op.create_index(
        "ix_predictions_source_event",
        "predictions",
        ["source", sa.text("event_at DESC")],
    )

    # Per-ticker historical lookup, partial so multi-ticker / macro
    # rows don't bloat the index. Same partial-WHERE pattern as 0043's
    # ix_news_signals_materiality_high.
    op.create_index(
        "ix_predictions_ticker_event",
        "predictions",
        ["ticker", sa.text("event_at DESC")],
        sqlite_where=sa.text("ticker IS NOT NULL"),
        postgresql_where=sa.text("ticker IS NOT NULL"),
    )

    # Per-source dedup, partial on non-NULL message_id so legacy /
    # manual-source rows without a stable key don't collide. Writer
    # contract (§2.2): ``v1|predictions|<source>|<entity-id>``.
    op.create_index(
        "ix_predictions_source_messageid",
        "predictions",
        ["source", "message_id"],
        unique=True,
        sqlite_where=sa.text("message_id IS NOT NULL"),
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )

    # Evaluator hot-path: "give me rows due for scoring that aren't
    # archived yet." Partial WHERE keeps the index small even after
    # the retention compact job (§9.1) flips long-tail rows to
    # archived=1.
    op.create_index(
        "ix_predictions_due_at",
        "predictions",
        ["evaluation_due_at"],
        sqlite_where=sa.text("archived = 0"),
        postgresql_where=sa.text("archived = 0"),
    )

    # Time-ordered scan for backfill cursors + reliability windowed
    # views (§4.1 rolling-window variant).
    op.create_index(
        "ix_predictions_event_at",
        "predictions",
        ["event_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_predictions_event_at", table_name="predictions"
    )
    op.drop_index(
        "ix_predictions_due_at", table_name="predictions"
    )
    op.drop_index(
        "ix_predictions_source_messageid", table_name="predictions"
    )
    op.drop_index(
        "ix_predictions_ticker_event", table_name="predictions"
    )
    op.drop_index(
        "ix_predictions_source_event", table_name="predictions"
    )
    op.drop_table("predictions")
