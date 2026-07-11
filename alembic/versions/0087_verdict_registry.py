"""verdicts — settled fleet verdict registry (defended-verdicts machinery).

Item B (2026-07-11): every deep-decision / adjudication ships as
verdict + conviction + falsifiers + revisit triggers. Re-runs on a
settled subject require a cited NEW fact hitting a recorded
falsifier/trigger — else the standing verdict is DEFENDED (no agent
spawn). Deterministic trigger checker unlocks (not launches)
re-evaluation via a needs-confirm inbox row.

Additive only. Seeds (ORCL run 198, SOFI/BMY/OPEN, VOR, OKLO/RKLB/ASTS)
land via ``scripts/seed_verdict_registry.py`` — no data in this migration.

Revision ID: 0087_verdict_registry
Revises: 0085_signal_stream_events
Create Date: 2026-07-11

NOTE: parallel lane 0086 (synthesis-aftermath section-preservation) is
expected to revise 0085 as well. When 0086 lands, rebase this
``down_revision`` onto the landed 0086 revision id (reviewer merges the
heads). Do NOT steal 0086.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0087_verdict_registry"
down_revision: str | None = "0085_signal_stream_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "verdicts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Ticker or subject key (e.g. "ORCL", "VOR").
        sa.Column("subject", sa.String(64), nullable=False),
        # BUY | ADD | HOLD | TRIM | SELL | WAIT (WAIT aliases HOLD/wait).
        sa.Column("verdict", sa.String(16), nullable=False),
        # HIGH | MED | LOW
        sa.Column("conviction", sa.String(16), nullable=False),
        # JSON list of prose falsifiers ("what new fact would change this").
        sa.Column("falsifiers_json", sa.Text(), nullable=True),
        # JSON list of typed revisit triggers:
        # [{kind, ...}] kind in price_below|price_above|metric_condition|dated_event
        sa.Column("revisit_triggers_json", sa.Text(), nullable=True),
        sa.Column("next_validation", sa.Date(), nullable=True),
        sa.Column(
            "source_decision_run_id",
            sa.Integer(),
            sa.ForeignKey("decision_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "settled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "superseded_by",
            sa.Integer(),
            sa.ForeignKey("verdicts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reasoning_md", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_verdicts_user_subject_settled",
        "verdicts",
        ["user_id", "subject", "settled"],
    )
    # At most one settled row per (user, subject).
    op.create_index(
        "uq_verdicts_user_subject_settled",
        "verdicts",
        ["user_id", "subject"],
        unique=True,
        sqlite_where=sa.text("settled = 1"),
        postgresql_where=sa.text("settled = true"),
    )


def downgrade() -> None:
    op.drop_index("uq_verdicts_user_subject_settled", table_name="verdicts")
    op.drop_index("ix_verdicts_user_subject_settled", table_name="verdicts")
    op.drop_table("verdicts")
