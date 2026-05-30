"""alpha_report_analyses — Discord long-form alpha report LLM analysis ledger.

Revision ID: 0058_alpha_report_analyses
Revises: 0057_inferred_life_event_findings
Create Date: 2026-05-30

The alpha_report_analyst (Opus, see ``argosy/agents/alpha_report_analyst.py``)
consumes long-form Discord posts (Meet Kevin "Morning Brief" / "Alpha Report"
style) that the deterministic ``extract_alpha_call_from_text`` regex cannot
classify — multi-page text with tone, structural picks, per-ticker signals,
and macro commentary. This migration lands the analyst's persistence + extends
two enum CHECKs so its downstream writes land safely.

Three operations
================

1. **Create ``alpha_report_analyses`` table.** Stores the full structured
   analysis (one row per news_signal). UNIQUE(news_signal_id) enforces
   "at most one analysis per signal" — the runner is idempotent on that
   key (returns the existing row on a re-run, never produces duplicate
   Predictions / MonitorFlags).

   Columns mirror the agent's ``AlphaReportAnalysis`` dataclass:
   macro_tone + macro_tone_confidence enums; key_themes / ticker_signals_json
   / structural_picks_json / cautions_json / index_targets_json are JSON
   columns guarded by ``json_valid`` CHECKs (corruption fails at write
   time, not at downstream-consumer time — same pattern as
   ``state_snapshots.state_json`` / ``inferred_life_event_findings.
   evidence_transaction_ids`` per migrations 0049 / 0057).

   ``agent_version`` (default ``'v1'``) is the replay-safety knob: when
   the analyst's prompt or output schema evolves, bump this to ``'v2'``
   on new runs so historical rows stay parseable under their original
   shape. The runner pins agent_version='v1' until it sees a need to
   evolve.

2. **Relax ``predictions.source`` CHECK** to admit ``discord_alpha_report``
   as a 12th source value. Same pattern as migrations 0049's
   ``monitor_flags.kind`` relaxation: SQLite needs ``batch_alter_table``
   to drop+recreate the CHECK; we copy the legacy 11 values from
   migration 0050 and add the new one. Per-tenant predictions ledger
   reads will now see ``source='discord_alpha_report'`` for predictions
   the analyst fanned out from a long-form post (one Prediction per
   ticker_signal + one per structural_pick).

3. **Relax ``monitor_flags.kind`` CHECK** to admit ``alpha_report_caution``
   as a 16th kind. Same shape as op #2 — preserves the 15 existing
   kinds from migration 0049 (3 legacy + 12 state_observer_*) and adds
   the new one. The analyst's runner writes a MonitorFlag with
   ``kind='alpha_report_caution'`` only when a parsed caution carries a
   severity hint reaching "warning"; lighter cautions stay in the
   analysis row's ``cautions_json`` only.

Pre-migration safety preflights
================================

Both CHECK relaxations follow the spec-§4 / migration-0049 precedent of
running a preflight that scans existing rows for any value that the
new enum would reject. SQLite's batch-rebuild silently drops violating
rows; the preflight raises ``RuntimeError`` with the offending values
listed so the operator can remediate before retrying.

For migration 0058 specifically — predictions.source values that aren't
in the existing 11-value set, and monitor_flags.kind values that
aren't in the existing 15-value set, would both be data-loss bugs.
The preflights run BEFORE any DDL so the migration is no-op-safe on
the failure path.

SQLite requirements: ``json_valid()`` needs >= 3.38 (already an Argosy
baseline). Partial indexes + batch_alter_table are unchanged from
migrations 0049-0057.

Downgrade
=========

Drops the alpha_report_analyses table (no data preservation — this
table is fresh-only in this migration). Re-installs the original
11-value predictions.source CHECK and the original 15-value
monitor_flags.kind CHECK. Symmetric preflights run before the
downgrade's batch_alter_tables — without them, ``discord_alpha_report``
predictions / ``alpha_report_caution`` monitor_flags would be silently
dropped on the constraint swap.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058_alpha_report_analyses"
down_revision: str | None = "0057_inferred_life_event_findings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Enum value tables — single source of truth for CHECK constraints.
# ---------------------------------------------------------------------------

# Per ``AlphaReportAnalysis.macro_tone`` — five buckets ranging from
# decisively bullish to decisively bearish.
_VALID_MACRO_TONES: tuple[str, ...] = (
    "bullish",
    "cautiously_bullish",
    "mixed",
    "cautiously_bearish",
    "bearish",
)

# Confidence band (low/medium/high) — applies to ``macro_tone_confidence``
# AND ``confidence_overall`` (same enum, two columns).
_VALID_CONFIDENCE_BANDS: tuple[str, ...] = ("low", "medium", "high")


# predictions.source — extend the migration-0050 list by ONE value.
# Order matches migration 0050 verbatim so the diff is just "+ one
# trailing entry" (easier git review). Re-bumping this list in a
# future migration follows the same +1 pattern.
_LEGACY_PREDICTION_SOURCES: tuple[str, ...] = (
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
_NEW_PREDICTION_SOURCE: str = "discord_alpha_report"
_ALL_PREDICTION_SOURCES: tuple[str, ...] = (
    *_LEGACY_PREDICTION_SOURCES,
    _NEW_PREDICTION_SOURCE,
)


# monitor_flags.kind — extend migration-0049's 15-value enum by ONE.
# Order again matches the source migration so git review is a one-line
# diff at the bottom.
_LEGACY_MF_KINDS: tuple[str, ...] = (
    # Legacy three (migration 0043).
    "allocation_drift",
    "mc_regression",
    "macro_shift",
    # The twelve state_observer_* kinds (migration 0049).
    "state_observer_fx_observation",
    "state_observer_rates_observation",
    "state_observer_equity_observation",
    "state_observer_volatility_observation",
    "state_observer_allocation_observation",
    "state_observer_position_observation",
    "state_observer_concentration_observation",
    "state_observer_cash_observation",
    "state_observer_cashflow_observation",
    "state_observer_tax_observation",
    "state_observer_plan_assumption_observation",
    "state_observer_other_observation",
)
_NEW_MF_KIND: str = "alpha_report_caution"
_ALL_MF_KINDS: tuple[str, ...] = (*_LEGACY_MF_KINDS, _NEW_MF_KIND)


def _quoted_csv(values: Sequence[str]) -> str:
    return ", ".join(repr(v) for v in values)


# ---------------------------------------------------------------------------
# Preflights — refuse to touch CHECKs if existing rows would be dropped.
# ---------------------------------------------------------------------------


def _preflight_predictions_source(allowed: Sequence[str]) -> None:
    """Refuse to rebuild ``predictions`` if a row would fail the new CHECK.

    Same data-loss guard as migration 0049's ``_preflight_kinds`` — without
    this, the SQLite batch-rebuild pattern would silently DROP rows whose
    ``source`` value isn't in ``allowed``. We list the offending distinct
    values so the operator can remediate (UPDATE the rows, or extend the
    enum further in a v2 migration).
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("predictions"):
        return

    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT source FROM predictions "
            "WHERE source NOT IN (" + _quoted_csv(allowed) + ")"
        )
    ).fetchall()
    unknown = sorted(r[0] for r in rows)
    if unknown:
        raise RuntimeError(
            "Migration 0058 preflight failed: predictions contains source "
            f"values that are not in the new CHECK enum: {unknown}. "
            "Remediate (UPDATE offending rows to a known source, or "
            "extend the allowed list in this migration) before retrying."
        )


def _preflight_monitor_flags_kind(allowed: Sequence[str]) -> None:
    """Mirror of the predictions preflight for the monitor_flags.kind CHECK."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("monitor_flags"):
        return

    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT kind FROM monitor_flags "
            "WHERE kind NOT IN (" + _quoted_csv(allowed) + ")"
        )
    ).fetchall()
    unknown = sorted(r[0] for r in rows)
    if unknown:
        raise RuntimeError(
            "Migration 0058 preflight failed: monitor_flags contains kind "
            f"values that are not in the new CHECK enum: {unknown}. "
            "Remediate (UPDATE offending rows to a known kind, or extend "
            "the allowed list in this migration) before retrying."
        )


# ---------------------------------------------------------------------------
# upgrade / downgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. alpha_report_analyses table.
    # ------------------------------------------------------------------
    op.create_table(
        "alpha_report_analyses",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "news_signal_id",
            sa.Integer,
            sa.ForeignKey("news_signals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "analyzed_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("macro_tone", sa.Text, nullable=False),
        sa.Column("macro_tone_confidence", sa.Text, nullable=False),
        sa.Column("key_themes", sa.Text, nullable=False),
        sa.Column("summary_rationale", sa.Text, nullable=False),
        sa.Column("ticker_signals_json", sa.Text, nullable=False),
        sa.Column("structural_picks_json", sa.Text, nullable=False),
        sa.Column("cautions_json", sa.Text, nullable=False),
        sa.Column("index_targets_json", sa.Text, nullable=False),
        sa.Column("confidence_overall", sa.Text, nullable=False),
        # Replay-safe schema evolution: bump on the runner when the prompt /
        # output shape changes so existing rows stay parseable under their
        # original version.
        sa.Column(
            "agent_version",
            sa.Text,
            nullable=False,
            server_default=sa.text("'v1'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # CHECK enums on the two band columns.
        sa.CheckConstraint(
            "macro_tone IN (" + _quoted_csv(_VALID_MACRO_TONES) + ")",
            name="ck_alpha_report_analyses_macro_tone",
        ),
        sa.CheckConstraint(
            "macro_tone_confidence IN ("
            + _quoted_csv(_VALID_CONFIDENCE_BANDS)
            + ")",
            name="ck_alpha_report_analyses_macro_tone_confidence",
        ),
        sa.CheckConstraint(
            "confidence_overall IN ("
            + _quoted_csv(_VALID_CONFIDENCE_BANDS)
            + ")",
            name="ck_alpha_report_analyses_confidence_overall",
        ),
        # JSON column shape guards — corrupted JSON fails at write time
        # instead of crashing the downstream consumer at read time.
        sa.CheckConstraint(
            "json_valid(key_themes)",
            name="ck_alpha_report_analyses_key_themes_json_valid",
        ),
        sa.CheckConstraint(
            "json_valid(ticker_signals_json)",
            name="ck_alpha_report_analyses_ticker_signals_json_valid",
        ),
        sa.CheckConstraint(
            "json_valid(structural_picks_json)",
            name="ck_alpha_report_analyses_structural_picks_json_valid",
        ),
        sa.CheckConstraint(
            "json_valid(cautions_json)",
            name="ck_alpha_report_analyses_cautions_json_valid",
        ),
        sa.CheckConstraint(
            "json_valid(index_targets_json)",
            name="ck_alpha_report_analyses_index_targets_json_valid",
        ),
        # At-most-one analysis per news_signal — runner idempotency
        # contract. Re-running on the same signal returns the existing
        # row (the runner SELECTs first; this UNIQUE is defence in
        # depth against a race).
        sa.UniqueConstraint(
            "news_signal_id",
            name="uq_alpha_report_analyses_news_signal_id",
        ),
    )

    # Hot-path index for the runner's "find all signals without an
    # analysis row" query — typically a small set.
    op.create_index(
        "ix_alpha_report_analyses_user_analyzed_at",
        "alpha_report_analyses",
        ["user_id", sa.text("analyzed_at DESC")],
    )

    # ------------------------------------------------------------------
    # 2. predictions.source CHECK relaxation — add 'discord_alpha_report'.
    # ------------------------------------------------------------------
    _preflight_predictions_source(_ALL_PREDICTION_SOURCES)

    with op.batch_alter_table("predictions") as batch:
        batch.drop_constraint("ck_predictions_source", type_="check")
        batch.create_check_constraint(
            "ck_predictions_source",
            f"source IN ({_quoted_csv(_ALL_PREDICTION_SOURCES)})",
        )

    # ------------------------------------------------------------------
    # 3. monitor_flags.kind CHECK relaxation — add 'alpha_report_caution'.
    # ------------------------------------------------------------------
    _preflight_monitor_flags_kind(_ALL_MF_KINDS)

    with op.batch_alter_table("monitor_flags") as batch:
        batch.drop_constraint("ck_monitor_flags_kind", type_="check")
        batch.create_check_constraint(
            "ck_monitor_flags_kind",
            f"kind IN ({_quoted_csv(_ALL_MF_KINDS)})",
        )


def downgrade() -> None:
    # Symmetric preflights — refuse if any row would be silently dropped
    # by reinstalling the original (narrower) CHECKs.
    _preflight_predictions_source(_LEGACY_PREDICTION_SOURCES)
    _preflight_monitor_flags_kind(_LEGACY_MF_KINDS)

    with op.batch_alter_table("monitor_flags") as batch:
        batch.drop_constraint("ck_monitor_flags_kind", type_="check")
        batch.create_check_constraint(
            "ck_monitor_flags_kind",
            f"kind IN ({_quoted_csv(_LEGACY_MF_KINDS)})",
        )

    with op.batch_alter_table("predictions") as batch:
        batch.drop_constraint("ck_predictions_source", type_="check")
        batch.create_check_constraint(
            "ck_predictions_source",
            f"source IN ({_quoted_csv(_LEGACY_PREDICTION_SOURCES)})",
        )

    op.drop_index(
        "ix_alpha_report_analyses_user_analyzed_at",
        table_name="alpha_report_analyses",
    )
    op.drop_table("alpha_report_analyses")
