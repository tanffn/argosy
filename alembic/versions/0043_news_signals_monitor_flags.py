"""news_signals + monitor_flags: daily-automation + monitor agent storage.

Revision ID: 0043_news_signals_monitor_flags
Revises: 0042_life_events
Create Date: 2026-05-29

Sprint commit #4 of the plan/execute/monitor reorg. Two tightly coupled
tables; one migration despite codex IMPORTANT #7 because they share a
producer/consumer pipeline:

  Stage 1 extractor (no LLM) → news_signals row
  Stage 2 analyst (LLM)      → updates news_signals with materiality
  Monitor agent              → reads news_signals, writes monitor_flags
  Home Red-Flag Strip        → reads monitor_flags

news_signals (spec §5.2):

  Each raw input (discord msg / RSS item / macro-feed entry) becomes
  one row. Stage 1 fills source/source_ref/received_at/parsed_tickers/
  event_keywords/sentiment/source_trust/evidence_excerpt/raw_text via
  pure regex + keyword lookup -- NO LLM. Stage 2 (analyst, Opus) then
  fills materiality/recommended_flag/rationale.

  The split is the codex-flagged prompt-injection isolation contract:
  raw_text is stored as evidence-only and NEVER reaches the LLM prompt.
  The analyst sees only the normalized fields (parsed_tickers,
  event_keywords, sentiment, source_trust). evidence_excerpt is the
  user-visible quote for citation display.

monitor_flags (spec §5.1):

  One row per active red-flag surface. Three kinds:
    allocation_drift  (drift trigger from §5.1.1)
    mc_regression     (monthly MC refresh, §5.1.2)
    macro_shift       (news-analyst-derived, §5.1.3)

  payload (JSON) carries the kind-specific detail; severity is the
  Red-Flag-Strip visual treatment driver. acknowledged_at tracks user
  dismissal; expires_at lets stale flags auto-clean (e.g. drift flag
  that re-fires next snapshot supersedes the old one).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_news_signals_monitor_flags"
down_revision: str | None = "0042_life_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VALID_NEWS_SOURCES = ("discord", "rss", "macro_feed")
_VALID_SENTIMENTS = ("positive", "neutral", "negative")
_VALID_TRUSTS = ("high", "medium", "low")
_VALID_MATERIALITIES = ("high", "medium", "low")
_VALID_FLAG_KINDS = ("allocation_drift", "mc_regression", "macro_shift")
_VALID_SEVERITIES = ("info", "warning", "critical")


def _enum_sql(values: tuple[str, ...]) -> str:
    return ", ".join(repr(v) for v in values)


def upgrade() -> None:
    # news_signals
    op.create_table(
        "news_signals",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        # Stage 1 fields (deterministic extractor)
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_ref", sa.Text, nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False
        ),
        # JSON list of tickers
        sa.Column(
            "parsed_tickers",
            sa.Text,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        # JSON list of event keywords (rate / Fed / earnings / ...)
        sa.Column(
            "event_keywords",
            sa.Text,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("sentiment", sa.String(16), nullable=False),
        sa.Column("source_trust", sa.String(8), nullable=False),
        # 280-char max — user-visible quote for citation display
        sa.Column("evidence_excerpt", sa.Text, nullable=False),
        # FULL raw text — STORED for citation but NEVER fed to LLM prompt
        # (codex BLOCKER #2 isolation contract).
        sa.Column("raw_text", sa.Text, nullable=False),
        # Stage 2 fields (analyst LLM) — null until analyzed
        sa.Column("materiality", sa.String(8), nullable=True),
        sa.Column("recommended_flag", sa.String(32), nullable=True),
        sa.Column("rationale", sa.Text, nullable=True),
        sa.Column(
            "analyzed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            f"source IN ({_enum_sql(_VALID_NEWS_SOURCES)})",
            name="ck_news_signals_source",
        ),
        sa.CheckConstraint(
            f"sentiment IN ({_enum_sql(_VALID_SENTIMENTS)})",
            name="ck_news_signals_sentiment",
        ),
        sa.CheckConstraint(
            f"source_trust IN ({_enum_sql(_VALID_TRUSTS)})",
            name="ck_news_signals_trust",
        ),
        sa.CheckConstraint(
            "materiality IS NULL OR materiality IN ("
            + _enum_sql(_VALID_MATERIALITIES)
            + ")",
            name="ck_news_signals_materiality",
        ),
        # Codex IMPORTANT: recommended_flag matches the monitor_flags
        # kind enum (analyst proposes a kind; monitor agent emits the
        # actual flag). Without this CHECK an invalid value would silently
        # persist.
        sa.CheckConstraint(
            "recommended_flag IS NULL OR recommended_flag IN ("
            + _enum_sql(_VALID_FLAG_KINDS)
            + ")",
            name="ck_news_signals_recommended_flag",
        ),
        sa.CheckConstraint(
            "length(evidence_excerpt) <= 280",
            name="ck_news_signals_excerpt_len",
        ),
    )
    # Source dedup — same source+source_ref shouldn't double-ingest.
    op.create_index(
        "ix_news_signals_source_ref",
        "news_signals",
        ["source", "source_ref"],
        unique=True,
    )
    op.create_index(
        "ix_news_signals_received",
        "news_signals",
        ["received_at"],
    )
    # Partial index on high-materiality signals — the monitor agent's
    # primary read path. SQLite + Postgres both support partial.
    # Codex NICE: `materiality` removed from index keys — the partial
    # predicate fixes it, so the leading column was redundant.
    op.create_index(
        "ix_news_signals_materiality_high",
        "news_signals",
        ["received_at"],
        sqlite_where=sa.text("materiality = 'high'"),
        postgresql_where=sa.text("materiality = 'high'"),
    )

    # monitor_flags
    op.create_table(
        "monitor_flags",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        # JSON kind-specific payload — drift includes (snapshot_date, row);
        # mc_regression includes (prev_p_solvent, curr_p_solvent, delta_pp);
        # macro_shift includes (news_signal_id, classifier_rationale).
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column(
            "surfaced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "acknowledged_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            f"kind IN ({_enum_sql(_VALID_FLAG_KINDS)})",
            name="ck_monitor_flags_kind",
        ),
        sa.CheckConstraint(
            f"severity IN ({_enum_sql(_VALID_SEVERITIES)})",
            name="ck_monitor_flags_severity",
        ),
        # Codex IMPORTANT: cheap integrity guard against bad writes.
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at >= surfaced_at",
            name="ck_monitor_flags_expiry_order",
        ),
    )
    # Active flags lookup — Red-Flag Strip query path.
    op.create_index(
        "ix_monitor_flags_user_active",
        "monitor_flags",
        ["user_id", "surfaced_at"],
        sqlite_where=sa.text("acknowledged_at IS NULL"),
        postgresql_where=sa.text("acknowledged_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_monitor_flags_user_active", table_name="monitor_flags"
    )
    op.drop_table("monitor_flags")
    op.drop_index(
        "ix_news_signals_materiality_high", table_name="news_signals"
    )
    op.drop_index("ix_news_signals_received", table_name="news_signals")
    op.drop_index("ix_news_signals_source_ref", table_name="news_signals")
    op.drop_table("news_signals")
