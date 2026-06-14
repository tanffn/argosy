"""Admit thesis_monitor_* kinds in the monitor_flags.kind CHECK.

The per-holding thesis monitor writes ``thesis_monitor_weakened`` and
``thesis_monitor_broken`` flags through the same monitor_flags table the
state-observer uses. The existing ``ck_monitor_flags_kind`` CHECK (set by
migration 0058) admits only the legacy/state-observer/alpha-report kinds, so a
real migrated DB would reject the thesis-monitor inserts. This relaxes the CHECK
by TWO values, following the 0058/0049 batch_alter_table + preflight precedent
(SQLite needs drop+recreate; the symmetric downgrade preflight refuses to
reinstall the narrower CHECK if any thesis_monitor_* row would be dropped).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067_thesis_monitor_flag_kinds"
down_revision: str | None = "0066_trend_scan_state"
branch_labels = None
depends_on = None


# The current (post-0058) 16-value set — single source of truth.
_PRE_THESIS_MF_KINDS: tuple[str, ...] = (
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
    # migration 0058.
    "alpha_report_caution",
)
_NEW_THESIS_KINDS: tuple[str, ...] = (
    "thesis_monitor_weakened",
    "thesis_monitor_broken",
)
_ALL_MF_KINDS: tuple[str, ...] = (*_PRE_THESIS_MF_KINDS, *_NEW_THESIS_KINDS)


def _quoted_csv(values: Sequence[str]) -> str:
    return ", ".join(repr(v) for v in values)


def _preflight_monitor_flags_kind(allowed: Sequence[str]) -> None:
    """Refuse to rebuild the CHECK if any existing row would be dropped."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("monitor_flags"):
        return
    rows = bind.execute(
        sa.text("SELECT DISTINCT kind FROM monitor_flags WHERE kind IS NOT NULL")
    ).fetchall()
    present = {r[0] for r in rows}
    unknown = present - set(allowed)
    if unknown:
        raise RuntimeError(
            "Migration 0067 preflight failed: monitor_flags contains kind "
            f"values that are not in the target CHECK enum: {sorted(unknown)}."
        )


def upgrade() -> None:
    _preflight_monitor_flags_kind(_ALL_MF_KINDS)
    with op.batch_alter_table("monitor_flags") as batch:
        batch.drop_constraint("ck_monitor_flags_kind", type_="check")
        batch.create_check_constraint(
            "ck_monitor_flags_kind",
            f"kind IN ({_quoted_csv(_ALL_MF_KINDS)})",
        )


def downgrade() -> None:
    _preflight_monitor_flags_kind(_PRE_THESIS_MF_KINDS)
    with op.batch_alter_table("monitor_flags") as batch:
        batch.drop_constraint("ck_monitor_flags_kind", type_="check")
        batch.create_check_constraint(
            "ck_monitor_flags_kind",
            f"kind IN ({_quoted_csv(_PRE_THESIS_MF_KINDS)})",
        )
