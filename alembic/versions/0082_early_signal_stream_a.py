"""Stream A recipient resolution, radar evidence, and 180-day scoring.

Revision ID: 0082_early_signal_stream_a
Revises: 0081_entry_backfilled_methods
Create Date: 2026-07-10
"""
from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0082_early_signal_stream_a"
down_revision: str | None = "0081_entry_backfilled_methods"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCES: tuple[str, ...] = (
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
    "discord_alpha_report",
)

_METHODS: tuple[tuple[str, int, str], ...] = (
    (
        "fixed_lookahead_180d",
        1,
        "True 180-calendar-day fixed-lookahead score for early-signal "
        "thesis predictions; unlike legacy writers, this method is not "
        "subject to the general 30-day cap.",
    ),
    (
        "fixed_lookahead_180d_entry_backfilled",
        2,
        "Entry-backfilled v2 of fixed_lookahead_180d; insert-only "
        "supersession for structurally unparseable v1 outcomes.",
    ),
)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(repr(value) for value in values)


def _source_reliability_sql() -> str:
    sibling = Path(__file__).resolve().parent / "0052_source_reliability_view.py"
    spec = importlib.util.spec_from_file_location(
        "argosy_alembic_0052_source_reliability_view", sibling
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load source reliability SQL from {sibling}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._VIEW_SQL  # type: ignore[attr-defined]


def upgrade() -> None:
    op.create_table(
        "signal_recipient_resolutions",
        sa.Column("recipient_normalized", sa.String(256), primary_key=True),
        sa.Column("recipient_name", sa.Text, nullable=False),
        sa.Column("ticker", sa.String(32), nullable=True),
        sa.Column("resolution_method", sa.String(16), nullable=False),
        sa.Column(
            "candidates_json",
            sa.Text,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "json_valid(candidates_json)",
            name="ck_signal_recipient_resolutions_candidates_json",
        ),
    )
    op.add_column(
        "trend_scan_state",
        sa.Column("nomination_evidence_json", sa.Text, nullable=True),
    )
    with op.batch_alter_table("trend_scan_state") as batch:
        batch.create_check_constraint(
            "ck_trend_scan_state_nomination_evidence_json_valid",
            "nomination_evidence_json IS NULL OR "
            "json_valid(nomination_evidence_json)",
        )
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
                "family": "fixed_lookahead",
                "method_version": version,
                "description": description,
                "is_active": 1,
            }
            for name, version, description in _METHODS
        ],
    )
    op.execute("DROP VIEW IF EXISTS source_reliability")
    with op.batch_alter_table("predictions") as batch:
        batch.drop_constraint("ck_predictions_source", type_="check")
        batch.create_check_constraint(
            "ck_predictions_source",
            f"source IN ({_quoted(_SOURCES)}) "
            "OR source LIKE 'signal_stream:%'",
        )
    op.execute(_source_reliability_sql())


def downgrade() -> None:
    bind = op.get_bind()
    signal_rows = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM predictions "
            "WHERE source LIKE 'signal_stream:%' "
            "OR evaluation_method LIKE 'fixed_lookahead_180d%'"
        )
    ).scalar_one()
    if signal_rows:
        raise RuntimeError(
            "Cannot downgrade Stream A while signal-stream/180d predictions "
            "exist; archive or migrate them first."
        )
    op.execute("DROP VIEW IF EXISTS source_reliability")
    with op.batch_alter_table("predictions") as batch:
        batch.drop_constraint("ck_predictions_source", type_="check")
        batch.create_check_constraint(
            "ck_predictions_source",
            f"source IN ({_quoted(_SOURCES)})",
        )
    op.execute(_source_reliability_sql())
    names = [name for name, _, _ in _METHODS]
    bind.execute(
        sa.text(
            "DELETE FROM prediction_outcomes "
            "WHERE evaluation_method IN :names"
        ).bindparams(sa.bindparam("names", expanding=True)),
        {"names": names},
    )
    bind.execute(
        sa.text(
            "DELETE FROM evaluation_method_registry "
            "WHERE method_name IN :names"
        ).bindparams(sa.bindparam("names", expanding=True)),
        {"names": names},
    )
    with op.batch_alter_table("trend_scan_state") as batch:
        batch.drop_constraint(
            "ck_trend_scan_state_nomination_evidence_json_valid",
            type_="check",
        )
        batch.drop_column("nomination_evidence_json")
    op.drop_table("signal_recipient_resolutions")
