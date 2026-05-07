"""decision_runs amendment columns + per-user running-amendment index (Wave 4).

Revision ID: 0018_decision_runs_amendment
Revises: 0017_plan_versions_synthesis
Create Date: 2026-05-07

Plan-amendment-chat (Wave 4) writes one ``decision_runs`` row per amendment
request. The advisor's classifier emits a *tier* of ``"small"``, ``"medium"``,
or ``"large"`` describing the scope of the requested change, and the row also
carries free-form ``notes_json`` (the user's amendment text + the parsed
``AmendmentIntent`` for replay).

Deviation from the Wave 4 plan: the original plan called for
``add_column("tier", ...)`` on ``decision_runs``. A ``tier`` column already
exists from migration 0004 — ``String(4)`` ``NOT NULL``, used for trade-tier
sentinels like ``"T0"`` and ``"T3"``. Rather than introduce a second
column with overlapping semantics, this migration *widens* the existing
column to ``String(8)`` and makes it nullable so the same column can carry
either a T-tier or an amendment-tier. Existing rows keep their values
(``"T0"``/``"T3"``); new amendment-chat runs write ``"small"``/``"medium"``/
``"large"``. The ``decision_kind`` column already disambiguates which value
shape is in play (``"trade_proposal"``/``"plan_revision"`` vs.
``"plan_amendment_chat"``).

The partial unique index ``ix_decision_runs_one_amendment_running_per_user``
enforces "one in-flight amendment per user" — only rows where
``decision_kind='plan_amendment_chat' AND status='running'`` participate, so
trade-proposal and plan-revision runs are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_decision_runs_amendment"
down_revision: str | Sequence[str] | None = "0017_plan_versions_synthesis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Widen the existing tier column (String(4) NOT NULL -> String(8) nullable)
    # so it can also carry amendment-tier values "small"/"medium"/"large".
    with op.batch_alter_table("decision_runs") as batch:
        batch.alter_column(
            "tier",
            existing_type=sa.String(length=4),
            type_=sa.String(length=8),
            existing_nullable=False,
            nullable=True,
        )
        batch.add_column(sa.Column("notes_json", sa.Text(), nullable=True))

    op.create_index(
        "ix_decision_runs_one_amendment_running_per_user",
        "decision_runs",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text(
            "decision_kind='plan_amendment_chat' AND status='running'"
        ),
        sqlite_where=sa.text(
            "decision_kind='plan_amendment_chat' AND status='running'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_decision_runs_one_amendment_running_per_user",
        table_name="decision_runs",
    )
    with op.batch_alter_table("decision_runs") as batch:
        batch.drop_column("notes_json")
        batch.alter_column(
            "tier",
            existing_type=sa.String(length=8),
            type_=sa.String(length=4),
            existing_nullable=True,
            nullable=False,
        )
