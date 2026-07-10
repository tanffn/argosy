"""Seed v2 entry-backfilled fixed-lookahead evaluation methods.

Backtest-unblock (2026-07-10): every ``discord_alpha_report`` prediction
was written with ``entry_price=NULL`` (the alpha-report fan-out writer
never snapshots an entry), so the v1 ``fixed_lookahead_*`` scorer
refused all 372 rows ("fixed_lookahead chosen but entry_price missing").
The fix is a re-evaluation path that backfills the entry as the
ticker's close on (or last trading day before) the prediction's
``event_at`` date — a standard backtest convention — and records the
outcome under a NEW method name so history is never mutated.

Supersession is the registry's existing mechanism: the
``source_reliability`` view dedups per ``(prediction_id, family)``
picking the highest ``method_version``, so these ``method_version=2``
rows cleanly supersede a prediction's v1 ``unparseable`` outcome while
the v1 row stays in ``prediction_outcomes`` as the audit trail.

Registry rows only — no schema change, no data backfill here. The
re-evaluation itself runs via
:func:`argosy.services.predictions.evaluator.run_reevaluation_batch`.

Revision ID: 0081_entry_backfilled_methods
Revises: 0080_position_stances
Create Date: 2026-07-10
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0081_entry_backfilled_methods"
down_revision: str | None = "0080_position_stances"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_V2_METHODS: tuple[tuple[str, str, int, str], ...] = (
    (
        "fixed_lookahead_7d_entry_backfilled",
        "fixed_lookahead",
        2,
        "v2 of fixed_lookahead_7d for predictions written without an "
        "entry_price snapshot: entry is backfilled as the ticker's "
        "close on (or last trading day before) event_at, then the "
        "spec §5.2 sign+magnitude classification applies unchanged. "
        "Supersedes the v1 unparseable outcome via the reliability "
        "view's method_version dedup.",
    ),
    (
        "fixed_lookahead_30d_entry_backfilled",
        "fixed_lookahead",
        2,
        "v2 of fixed_lookahead_30d for predictions written without an "
        "entry_price snapshot: entry is backfilled as the ticker's "
        "close on (or last trading day before) event_at, then the "
        "spec §5.2 sign+magnitude classification applies unchanged. "
        "Supersedes the v1 unparseable outcome via the reliability "
        "view's method_version dedup.",
    ),
)


def upgrade() -> None:
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
            for (name, family, version, description) in _V2_METHODS
        ],
    )


def downgrade() -> None:
    names = ", ".join(f"'{name}'" for (name, _, _, _) in _V2_METHODS)
    # Outcome rows written under the v2 methods FK-reference the
    # registry rows — remove them first. This deletes re-evaluation
    # results only; the v1 outcome rows (the pre-backfill history)
    # are untouched.
    op.execute(
        sa.text(
            "DELETE FROM prediction_outcomes "
            f"WHERE evaluation_method IN ({names})"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM evaluation_method_registry "
            f"WHERE method_name IN ({names})"
        )
    )
