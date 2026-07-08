"""Relax ck_action_proposals_status: add 'executed'.

Corrective (critique-fed) re-synthesis — docs/design/corrective_resynthesis.md
§2.C.3: on /accept of a corrective draft, the proposals the run fed (the
aggregated ``critique_resynth:{user_id}`` replan_full row + the accepted
adjudication directives, e.g. the glide-schedule verdict) flip to
``status='executed'`` so the inbox/critique panel can render "cleared by
draft #N" instead of leaving a satisfied proposal open forever.

The 0055 CHECK admits only open/accepted/deferred/rejected/superseded — a
terminal "the system itself applied this" state did not exist (the
capability-boundary design deliberately kept the PROPOSER from writing one;
this transition happens only in the promote hook, after the user's /accept).

Same CHECK-relaxation shape as migrations 0049 and 0077.

Revision ID: 0078_action_proposal_status_executed
Revises: 0077_action_proposal_kind_relax
"""
from __future__ import annotations

from typing import Sequence

from alembic import op

revision: str = "0078_action_proposal_status_executed"
down_revision: str | None = "0077_action_proposal_kind_relax"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_STATUSES: tuple[str, ...] = (
    "open",
    "accepted",
    "deferred",
    "rejected",
    "superseded",
)

_NEW_STATUSES: tuple[str, ...] = _LEGACY_STATUSES + ("executed",)


def _quoted_csv(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    with op.batch_alter_table("action_proposals") as batch:
        batch.drop_constraint("ck_action_proposals_status", type_="check")
        batch.create_check_constraint(
            "ck_action_proposals_status",
            f"status IN ({_quoted_csv(_NEW_STATUSES)})",
        )


def downgrade() -> None:
    # Demote rows carrying the new status first — the legacy CHECK cannot
    # round-trip them through the batch rebuild. 'accepted' is the closest
    # legacy semantic (user said yes; application state is lost).
    op.execute(
        "UPDATE action_proposals SET status = 'accepted' "
        "WHERE status = 'executed'"
    )
    with op.batch_alter_table("action_proposals") as batch:
        batch.drop_constraint("ck_action_proposals_status", type_="check")
        batch.create_check_constraint(
            "ck_action_proposals_status",
            f"status IN ({_quoted_csv(_LEGACY_STATUSES)})",
        )
