"""allocation_actions: create table (supersedes the unmigrated windfall_actions).

Revision ID: 0041_allocation_actions_rename
Revises: 0040_portfolio_snapshot_parts_partial_unique
Create Date: 2026-05-29

Background: a ``windfall_actions`` ORM class shipped in commit 3fe089c
(2026-05-29) but never came with an alembic migration; tests use
``Base.metadata.create_all`` and dev DBs sit at alembic head 0040 with
no such table. The plan/execute/monitor reorg spec
(``docs/superpowers/specs/2026-05-29-plan-execute-monitor-reorg-design.md``)
generalizes the shape to hold decisions from any allocation flow
(windfall + unallocated cash + monitor drift + life events + rebalance
+ manual), renaming the class to ``AllocationAction``. Codex tandem
review (BLOCKER #2) confirmed this is the right shape -- the table
must stay separate from the trade-order ``proposals`` table.

This migration is the first time the table lands as an alembic-managed
schema object. It handles two starting states:

  (a) Fresh DB, no windfall_actions row -- the typical path. Create
      allocation_actions with the new schema (action_source column +
      source_ref + source_detected_at).

  (b) Legacy DB where ``windfall_actions`` somehow got created out-of-
      band (Base.metadata.create_all run; older sandbox). Rename it
      then alter to the new shape, backfilling action_source='windfall'
      and copying event_source_tsv -> source_ref.

The dev DB is in state (a) per inspection on 2026-05-29.

CHECK constraint on action_source enforces the enum at the DB layer.
Partial unique index on (user_id, action_source, source_ref) -- without
decided_at -- guards against any duplicate Accept/Defer on the same
source row (codex IMPORTANT #2: including decided_at in the key let two
clicks at different timestamps both persist, contradicting the dedup
intent). NULL source_ref is the 'manual' case, allowed to recur. If the
route wants to allow a user to change an earlier decision, it does
UPDATE not INSERT.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_allocation_actions_rename"
down_revision: str | None = "0040_portfolio_snapshot_parts_partial_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VALID_SOURCES = (
    "windfall",
    "unallocated_cash",
    "monitor_drift",
    "rebalance",
    "life_event",
    "manual",
)
_SOURCES_SQL = ", ".join(repr(s) for s in _VALID_SOURCES)


def _create_indexes() -> None:
    op.create_index(
        "ix_allocation_actions_user_decided",
        "allocation_actions",
        ["user_id", "decided_at"],
    )
    op.create_index(
        "ix_allocation_actions_source_unique",
        "allocation_actions",
        ["user_id", "action_source", "source_ref"],
        unique=True,
        sqlite_where=sa.text("source_ref IS NOT NULL"),
        postgresql_where=sa.text("source_ref IS NOT NULL"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    has_old = inspector.has_table("windfall_actions")
    has_new = inspector.has_table("allocation_actions")

    if has_new:
        # Already migrated. Detect split-brain: if windfall_actions ALSO
        # still exists, raise loudly so the operator notices manual
        # cleanup is needed (codex IMPORTANT #1 — silent return masked
        # this case).
        if has_old:
            raise RuntimeError(
                "Split-brain schema detected: both 'windfall_actions' "
                "and 'allocation_actions' tables exist. The 0041 "
                "migration cannot reconcile this automatically. Manual "
                "cleanup required: inspect both tables, copy any "
                "missing rows from windfall_actions to allocation_actions "
                "(setting action_source='windfall', source_ref from "
                "event_source_tsv), then DROP TABLE windfall_actions and "
                "stamp alembic to 0041 if not already."
            )
        return

    if not has_old:
        # State (a): fresh DB. Create allocation_actions directly.
        op.create_table(
            "allocation_actions",
            sa.Column(
                "id", sa.Integer, primary_key=True, autoincrement=True
            ),
            sa.Column(
                "user_id",
                sa.String(64),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("action_source", sa.String(32), nullable=False),
            sa.Column(
                "source_detected_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column("source_ref", sa.Text, nullable=True),
            sa.Column("horizon", sa.String(8), nullable=False),
            sa.Column("asset_class", sa.String(64), nullable=False),
            sa.Column("instrument", sa.String(64), nullable=False),
            sa.Column("amount_usd", sa.Numeric(12, 2), nullable=False),
            sa.Column("rationale", sa.Text, nullable=False),
            sa.Column(
                "closes_delta_usd", sa.Numeric(12, 2), nullable=False
            ),
            sa.Column("confidence", sa.String(8), nullable=False),
            sa.Column(
                "decided_status",
                sa.String(16),
                nullable=False,
                server_default="accepted",
            ),
            sa.Column(
                "decided_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.Column("due_date", sa.Date, nullable=True),
            sa.Column("user_note", sa.Text, nullable=True),
            sa.Column(
                "proposal_id",
                sa.Integer,
                sa.ForeignKey("proposals.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.CheckConstraint(
                f"action_source IN ({_SOURCES_SQL})",
                name="ck_allocation_actions_source",
            ),
        )
        _create_indexes()
        return

    # State (b): legacy DB has windfall_actions but not allocation_actions.
    # Rename + alter dance.
    op.rename_table("windfall_actions", "allocation_actions")

    with op.batch_alter_table("allocation_actions") as batch:
        batch.add_column(
            sa.Column("action_source", sa.String(32), nullable=True)
        )
        batch.add_column(sa.Column("source_ref", sa.Text(), nullable=True))
        batch.alter_column(
            "event_detected_at", new_column_name="source_detected_at"
        )

    op.execute(
        sa.text(
            "UPDATE allocation_actions "
            "SET action_source = 'windfall', source_ref = event_source_tsv "
            "WHERE action_source IS NULL"
        )
    )

    with op.batch_alter_table("allocation_actions") as batch:
        batch.drop_column("event_source_tsv")
        batch.alter_column(
            "action_source", existing_type=sa.String(32), nullable=False
        )
        batch.create_check_constraint(
            "ck_allocation_actions_source",
            f"action_source IN ({_SOURCES_SQL})",
        )

    # Drop legacy indexes if present (only exist on state (b) DBs).
    for legacy_idx in (
        "ix_windfall_actions_event",
        "ix_windfall_actions_user_decided",
    ):
        try:
            op.drop_index(legacy_idx, table_name="allocation_actions")
        except sa.exc.OperationalError:
            # Index didn't exist on this DB; safe to skip.
            pass

    _create_indexes()


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("allocation_actions"):
        return

    op.drop_index(
        "ix_allocation_actions_source_unique",
        table_name="allocation_actions",
    )
    op.drop_index(
        "ix_allocation_actions_user_decided",
        table_name="allocation_actions",
    )
    op.drop_table("allocation_actions")
