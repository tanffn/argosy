"""Admit generic signal warnings and tenant-scope prediction dedup.

The prediction index keeps its established name and partial predicate, but
changes from UNIQUE(source, message_id) to
UNIQUE(user_id, source, message_id). Downgrade refuses when cross-user rows
would collide under the old shape.

Revision ID: 0084_signal_stream_warning_kind
Revises: 0083_signal_stream_cursors
Create Date: 2026-07-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0084_signal_stream_warning_kind"
down_revision: str | None = "0083_signal_stream_cursors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRE_SIGNAL_WARNING_KINDS: tuple[str, ...] = (
    "allocation_drift",
    "mc_regression",
    "macro_shift",
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
    "alpha_report_caution",
    "thesis_monitor_weakened",
    "thesis_monitor_broken",
)
_ALL_MONITOR_FLAG_KINDS: tuple[str, ...] = (
    *_PRE_SIGNAL_WARNING_KINDS,
    "signal_stream_warning",
)
_PREDICTION_DEDUP_INDEX = "ix_predictions_source_messageid"


def _quoted_csv(values: Sequence[str]) -> str:
    return ", ".join(repr(value) for value in values)


def _preflight_monitor_flags_kind(allowed: Sequence[str]) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("monitor_flags"):
        return
    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT kind FROM monitor_flags "
            "WHERE kind IS NOT NULL"
        )
    ).fetchall()
    unknown = {row[0] for row in rows} - set(allowed)
    if unknown:
        raise RuntimeError(
            "Migration 0084 preflight failed: monitor_flags contains kind "
            "values that are not in the target CHECK enum: "
            f"{sorted(unknown)}."
        )


def _replace_kind_check(allowed: Sequence[str]) -> None:
    with op.batch_alter_table("monitor_flags") as batch:
        batch.drop_constraint("ck_monitor_flags_kind", type_="check")
        batch.create_check_constraint(
            "ck_monitor_flags_kind",
            f"kind IN ({_quoted_csv(allowed)})",
        )


def _replace_prediction_dedup_index(*, include_user_id: bool) -> None:
    op.drop_index(_PREDICTION_DEDUP_INDEX, table_name="predictions")
    columns = (
        ["user_id", "source", "message_id"]
        if include_user_id
        else ["source", "message_id"]
    )
    op.create_index(
        _PREDICTION_DEDUP_INDEX,
        "predictions",
        columns,
        unique=True,
        sqlite_where=sa.text("message_id IS NOT NULL"),
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )


def _preflight_prediction_downgrade() -> None:
    bind = op.get_bind()
    collision = bind.execute(
        sa.text(
            "SELECT source, message_id, COUNT(DISTINCT user_id) AS users "
            "FROM predictions "
            "WHERE message_id IS NOT NULL "
            "GROUP BY source, message_id "
            "HAVING COUNT(DISTINCT user_id) > 1 "
            "LIMIT 1"
        )
    ).first()
    if collision is not None:
        raise RuntimeError(
            "Migration 0084 prediction index downgrade preflight failed: "
            "cross-user rows would collide under UNIQUE(source, message_id): "
            f"source={collision[0]!r}, message_id={collision[1]!r}, "
            f"users={collision[2]}."
        )


def upgrade() -> None:
    _preflight_monitor_flags_kind(_ALL_MONITOR_FLAG_KINDS)
    _replace_kind_check(_ALL_MONITOR_FLAG_KINDS)
    _replace_prediction_dedup_index(include_user_id=True)


def downgrade() -> None:
    _preflight_monitor_flags_kind(_PRE_SIGNAL_WARNING_KINDS)
    _preflight_prediction_downgrade()
    _replace_kind_check(_PRE_SIGNAL_WARNING_KINDS)
    _replace_prediction_dedup_index(include_user_id=False)
