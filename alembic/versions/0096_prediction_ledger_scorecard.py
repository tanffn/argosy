"""Prediction ledger scorecard — fleet source + discord retirement.

Revision ID: 0096_prediction_ledger_scorecard
Revises: 0094_expense_tag_rules

Stream C (2026-08-07), review iterations 1–2:

1. Admit ``internal_fleet_verdict`` on ``ck_predictions_source``.

2. Retire dead Discord *ingestion* without erasing unfinished scoreable
   history: archive only rows whose outcomes are exclusively
   ``unparseable`` (already graded unusable). Zero-outcome overdue rows
   stay active for the evaluator — price history exists without Discord.

3. ``predictions.superseded_by_prediction_id`` — lineage pointer so
   corrections append a new immutable version instead of mutating.

4. ``prediction_evaluator_batch_failures`` — durable evidence when a
   due batch grades zero usable outcomes (survives tick rollback).

5. ``source_reliability`` VIEW is LEFT UNCHANGED (recreated from 0052).
   The standing scorecard owns its own exclusions in Python.

Do NOT apply this migration to the live DB from this stream — write only.
"""
from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0096_prediction_ledger_scorecard"
down_revision: str | None = "0094_expense_tag_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RETIRED_SOURCES: tuple[str, ...] = ("discord", "discord_alpha_report")

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
    "internal_fleet_verdict",
)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(repr(value) for value in values)


def _view_sql() -> str:
    """Canonical source_reliability SQL from migration 0052 (unchanged)."""
    import importlib.util
    from pathlib import Path

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
        "prediction_source_retirements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "retired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "prediction_ids_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "reversible",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.UniqueConstraint("source", name="uq_prediction_source_retirements_source"),
    )

    op.create_table(
        "prediction_evaluator_batch_failures",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("due_selected", sa.Integer(), nullable=False),
        sa.Column("unparseable", sa.Integer(), nullable=False),
        sa.Column("adapter_errors", sa.Integer(), nullable=False),
        sa.Column("overdue_unscored_remaining", sa.Integer(), nullable=False),
        sa.Column("prediction_ids_json", sa.Text(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=False),
    )

    # Recreate the CHECK + view. View SQL is the 0052 original — weight
    # formula is NOT changed in this stream (review item 4).
    op.execute("DROP VIEW IF EXISTS source_reliability")
    with op.batch_alter_table("predictions") as batch:
        batch.drop_constraint("ck_predictions_source", type_="check")
        batch.create_check_constraint(
            "ck_predictions_source",
            f"source IN ({_quoted(_SOURCES)}) "
            "OR source LIKE 'signal_stream:%'",
        )
        batch.add_column(
            sa.Column(
                "superseded_by_prediction_id",
                sa.Integer(),
                nullable=True,
            )
        )
    op.execute(_view_sql())

    bind = op.get_bind()
    reason = (
        "Stream C 2026-08-07: discord_listener dead since 2026-06-21 "
        "(gateway close 4004 Authentication failed; 5512/5603 error runs). "
        "discord_listener_enabled remains False. Archiving ONLY discord* "
        "rows whose outcomes are exclusively unparseable (genuinely "
        "unscoreable). Zero-outcome overdue rows stay active for scoring."
    )
    for source in _RETIRED_SOURCES:
        # Archive only already-graded-unusable rows. Unscored rows with
        # a ticker remain due — retiring ingestion must not erase unfinished
        # scoreable history (iter-2 finding 1).
        rows = bind.execute(
            sa.text(
                """
                SELECT p.id
                FROM predictions p
                WHERE p.source = :source
                  AND p.archived = 0
                  AND EXISTS (
                    SELECT 1 FROM prediction_outcomes o
                    WHERE o.prediction_id = p.id
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM prediction_outcomes o
                    WHERE o.prediction_id = p.id
                      AND o.outcome_kind != 'unparseable'
                  )
                """
            ),
            {"source": source},
        ).fetchall()
        ids = [int(r[0]) for r in rows]
        if ids:
            chunk = 400
            for i in range(0, len(ids), chunk):
                part = ids[i : i + chunk]
                placeholders = ", ".join(str(x) for x in part)
                bind.execute(
                    sa.text(
                        f"UPDATE predictions SET archived = 1 "
                        f"WHERE id IN ({placeholders})"
                    )
                )
        bind.execute(
            sa.text(
                """
                INSERT INTO prediction_source_retirements
                    (source, reason, prediction_ids_json, reversible)
                VALUES (:source, :reason, :ids, 1)
                """
            ),
            {
                "source": source,
                "reason": reason,
                "ids": json.dumps(ids),
            },
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Fail closed: fleet predictions cannot survive a CHECK that forbids
    # their source (0082 preflight pattern).
    fleet_n = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM predictions "
            "WHERE source = 'internal_fleet_verdict'"
        )
    ).scalar_one()
    if int(fleet_n or 0) > 0:
        raise RuntimeError(
            "Cannot downgrade 0096 while internal_fleet_verdict predictions "
            f"exist (n={fleet_n}); archive or migrate them first."
        )

    rows = bind.execute(
        sa.text(
            "SELECT source, prediction_ids_json, reversible "
            "FROM prediction_source_retirements"
        )
    ).fetchall()
    for source, ids_json, reversible in rows:
        if not reversible:
            continue
        try:
            ids = json.loads(ids_json)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Cannot downgrade 0096: unreadable restoration map for "
                f"source={source!r}; refusing to drop prediction_source_"
                f"retirements (would leave rows archived with no reverse map)."
            ) from exc
        if not isinstance(ids, list):
            raise RuntimeError(
                f"Cannot downgrade 0096: restoration map for source="
                f"{source!r} is not a JSON list."
            )
        chunk = 400
        for i in range(0, len(ids), chunk):
            part = [int(x) for x in ids[i : i + chunk]]
            if not part:
                continue
            placeholders = ", ".join(str(x) for x in part)
            bind.execute(
                sa.text(
                    f"UPDATE predictions SET archived = 0 "
                    f"WHERE id IN ({placeholders}) AND source = :source"
                ),
                {"source": source},
            )

    op.drop_table("prediction_source_retirements")
    op.drop_table("prediction_evaluator_batch_failures")

    prior_sources = tuple(s for s in _SOURCES if s != "internal_fleet_verdict")
    op.execute("DROP VIEW IF EXISTS source_reliability")
    with op.batch_alter_table("predictions") as batch:
        batch.drop_constraint("ck_predictions_source", type_="check")
        batch.create_check_constraint(
            "ck_predictions_source",
            f"source IN ({_quoted(prior_sources)}) "
            "OR source LIKE 'signal_stream:%'",
        )
        batch.drop_column("superseded_by_prediction_id")
    op.execute(_view_sql())
