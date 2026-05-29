"""life_events: structured intake table for /life-events page.

Revision ID: 0042_life_events
Revises: 0041_allocation_actions_rename
Create Date: 2026-05-29

Sprint commit #3 of the plan/execute/monitor reorg. Backs the new
/life-events structured intake form (spec §4) where the user records
career / family / asset / expense / recurring / retirement-milestone
events that feed:

  - cashflow_projection.effective_retire_ready_age() — clamps the
    retire-ready age by retirement_milestone:target_retire_year_change
    + blocking expense_event entries.
  - <HolisticTimelineCard> on /retirement — renders all life events as
    timeline markers.
  - Monitor agent — reads life events as context for drift / MC
    interpretation (does NOT fire its own life-event-proximity flags;
    user's Q2 answer was explicit on that).

Category-level enum enforced via CHECK constraint at the DB layer; the
per-category kind enum is enforced by Pydantic at the service layer
(see argosy/services/life_events.py, sprint commit #8). DB stores kind
as TEXT because the valid kinds vary by category — encoding that
relationship at the DB level would require a multi-column CHECK or a
join table, neither of which adds material safety over the Pydantic
contract.

amount_usd + recurring_years + target_date are all nullable: not every
kind carries every field. Example: a marriage event has target_date
but no amount_usd; a recurring new-car expense has amount_usd +
recurring_years but no target_date (the next occurrence is computed).

Codex review (sprint commit #3) flagged two additional DB-level checks
for data quality:
  - amount_usd, when present, must be > 0 — the convention is "amount
    is a positive magnitude; direction is implicit in the kind"
    (home_sale proceeds and home_purchase cost both stored positive).
  - recurring_years, when present, must be > 0 — zero or negative
    has no physical meaning for "happens every N years".

`updated_at` behavior: triggered by SQLAlchemy's `onupdate=_utcnow` at
the ORM layer only. Direct SQL UPDATEs (bulk maintenance, alembic data
migrations) do NOT auto-update this column — callers must set it
explicitly. For v1 all writes go through `argosy/services/life_events.py`
which is ORM-only, so the policy is sufficient.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_life_events"
down_revision: str | None = "0041_allocation_actions_rename"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VALID_CATEGORIES = (
    "career_event",
    "family_event",
    "asset_event",
    "expense_event",
    "recurring_expense",
    "retirement_milestone",
)


def upgrade() -> None:
    op.create_table(
        "life_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("target_date", sa.Date, nullable=True),
        sa.Column("amount_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("recurring_years", sa.Integer, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        # source_id is a forward-looking FK candidate; for v1 it's just
        # a free integer reference (e.g. file_catalog row id when a user
        # links a doc to the event). No FK constraint until the linked-
        # source UX lands.
        sa.Column("source_id", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "category IN ("
            + ", ".join(repr(c) for c in _VALID_CATEGORIES)
            + ")",
            name="ck_life_events_category",
        ),
        # Data-quality checks per codex review of sprint commit #3.
        sa.CheckConstraint(
            "amount_usd IS NULL OR amount_usd > 0",
            name="ck_life_events_amount_positive",
        ),
        sa.CheckConstraint(
            "recurring_years IS NULL OR recurring_years > 0",
            name="ck_life_events_recurring_positive",
        ),
    )
    op.create_index(
        "ix_life_events_user_date",
        "life_events",
        ["user_id", "target_date"],
    )
    op.create_index(
        "ix_life_events_user_category",
        "life_events",
        ["user_id", "category"],
    )


def downgrade() -> None:
    op.drop_index("ix_life_events_user_category", table_name="life_events")
    op.drop_index("ix_life_events_user_date", table_name="life_events")
    op.drop_table("life_events")
