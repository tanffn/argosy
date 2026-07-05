"""Relax ck_action_proposals_kind: add stock_decision + deploy_team_flag.

Live finding (2026-07-05): both inbox sinks that write ActionProposal rows with
new kinds were silently dead — ``write_stock_decision_proposal``
(kind="stock_decision", shipped with the holdings-review capability) and
``write_team_flag_proposals`` (kind="deploy_team_flag", the deploy decision-team
flag sink). The 0055 CHECK constraint allows only the original 8 v1 kinds, so
every insert died with ``sqlite3.IntegrityError: CHECK constraint failed`` —
which both sinks swallow as a presumed dedup collision. The unit tests used a
fake db object, so the constraint never fired in CI: green tests, dead feature.

Same CHECK-relaxation shape as migration 0049 (monitor_flags.kind).

Revision ID: 0077_action_proposal_kind_relax
Revises: 0076_plan_allocation_overrides
"""
from __future__ import annotations

from typing import Sequence

from alembic import op

revision: str = "0077_action_proposal_kind_relax"
down_revision: str | None = "0076_plan_allocation_overrides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_KINDS: tuple[str, ...] = (
    "allocate",
    "repatriate_currency",
    "rebalance",
    "replan_full",
    "add_life_event_phase",
    "update_plan_assumption",
    "set_watchlist",
    "note_only",
)

_NEW_KINDS: tuple[str, ...] = _LEGACY_KINDS + (
    "stock_decision",
    "deploy_team_flag",
)


def _quoted_csv(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    with op.batch_alter_table("action_proposals") as batch:
        batch.drop_constraint("ck_action_proposals_kind", type_="check")
        batch.create_check_constraint(
            "ck_action_proposals_kind",
            f"kind IN ({_quoted_csv(_NEW_KINDS)})",
        )


def downgrade() -> None:
    # Remove rows carrying the new kinds first — the legacy CHECK cannot
    # round-trip them through the batch rebuild.
    op.execute(
        "DELETE FROM action_proposals "
        "WHERE kind IN ('stock_decision', 'deploy_team_flag')"
    )
    with op.batch_alter_table("action_proposals") as batch:
        batch.drop_constraint("ck_action_proposals_kind", type_="check")
        batch.create_check_constraint(
            "ck_action_proposals_kind",
            f"kind IN ({_quoted_csv(_LEGACY_KINDS)})",
        )
