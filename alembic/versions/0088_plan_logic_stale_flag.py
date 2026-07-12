"""Admit plan_logic_stale monitor flag kind (fact-token staleness seam).

When READ-time ``{{fact:key}}`` rendering detects that a live number crossed
a recorded FI claim boundary (prose says reached, book says not — or shock
needs a qualifier the sentence lacks), the renderer writes a
``plan_logic_stale`` monitor flag instead of shipping a contradiction.

Revision ID: 0088_plan_logic_stale_flag
Revises: 0087_verdict_registry
Create Date: 2026-07-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0088_plan_logic_stale_flag"
down_revision: str | None = "0087_verdict_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRE: tuple[str, ...] = (
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
    "signal_stream_warning",
)
_ALL: tuple[str, ...] = (*_PRE, "plan_logic_stale")


def _quoted_csv(values: Sequence[str]) -> str:
    return ", ".join(repr(value) for value in values)


def _preflight(allowed: Sequence[str]) -> None:
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
            "Migration 0088 preflight failed: monitor_flags contains kind "
            f"values not in the target CHECK enum: {sorted(unknown)}."
        )


def _replace(allowed: Sequence[str]) -> None:
    with op.batch_alter_table("monitor_flags") as batch:
        batch.drop_constraint("ck_monitor_flags_kind", type_="check")
        batch.create_check_constraint(
            "ck_monitor_flags_kind",
            f"kind IN ({_quoted_csv(allowed)})",
        )


def upgrade() -> None:
    _preflight(_ALL)
    _replace(_ALL)


def downgrade() -> None:
    _preflight(_PRE)
    _replace(_PRE)
