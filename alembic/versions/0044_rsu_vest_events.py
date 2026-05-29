"""rsu_vest_events: historical RSU vest event log from Schwab Equity Awards.

Revision ID: 0044_rsu_vest_events
Revises: 0043_news_signals_monitor_flags
Create Date: 2026-05-29

Sprint commit #7 of the plan/execute/monitor reorg. Backs the HolisticTimelineCard
(commit #10) + the RSU pre-vest planning surface (spec #2 commit #12).

**Spec revision vs original draft (cbf6a07 §3.2):** the original table name
`rsu_unvested_grants` implied future-only vest data, but the Schwab Equity
Awards Center CSV (verified against a real export 2026-05-29) does NOT
include future vest events. The CSV records HISTORICAL vest events via
paired `Lapse` + `Deposit` action rows. Upcoming-vest planning is therefore
computed by projecting forward from the per-grant historical cadence
(separate concern, lands as a service function not a persisted table).

This table holds the historical truth. One row per restriction-lapse event
(the canonical "shares vested on date X" record). Sourced from the CSV's
`Lapse` rows + their continuation sub-rows.

Schema fields:
  - grant_id          — Schwab AwardId (e.g. "289173", "213000"). Stable
                        across vest tranches of the same grant.
  - vest_date         — the date restriction lapsed (NOT the deposit date,
                        which is typically 1 day later; we use the Lapse
                        date as the canonical vest event).
  - shares_vested     — gross share count for this tranche (pre tax
                        withholding).
  - shares_withheld   — shares Schwab withheld for taxes (from Lapse
                        sub-row's `SharesSoldWithheldForTaxes` column).
  - shares_net        — net shares deposited into the brokerage account
                        (= shares_vested - shares_withheld).
  - fmv_per_share_usd — fair market value per share at vest (drives the
                        tax-cost calculation that pre-vest planning needs).
  - award_date        — when the original grant was awarded (drives
                        the projected-cadence inference for upcoming vests).
  - source_file       — provenance pointer (path of the parsed CSV).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_rsu_vest_events"
down_revision: str | None = "0043_news_signals_monitor_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rsu_vest_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("grant_id", sa.String(32), nullable=False),
        sa.Column("vest_date", sa.Date, nullable=False),
        sa.Column("shares_vested", sa.Numeric(16, 4), nullable=False),
        sa.Column("shares_withheld", sa.Numeric(16, 4), nullable=False),
        sa.Column("shares_net", sa.Numeric(16, 4), nullable=False),
        sa.Column("fmv_per_share_usd", sa.Numeric(12, 4), nullable=False),
        sa.Column("award_date", sa.Date, nullable=True),
        sa.Column("source_file", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Data quality: counts are non-negative; shares_vested ≥ shares_net
        # since shares_withheld is the difference. Strict equality not
        # enforced because tax withholding rounding can move a fractional
        # share between the two.
        sa.CheckConstraint(
            "shares_vested >= 0", name="ck_rsu_vest_shares_nonneg"
        ),
        sa.CheckConstraint(
            "shares_withheld >= 0", name="ck_rsu_vest_withheld_nonneg"
        ),
        sa.CheckConstraint(
            "shares_net >= 0", name="ck_rsu_vest_net_nonneg"
        ),
        sa.CheckConstraint(
            "fmv_per_share_usd > 0", name="ck_rsu_vest_fmv_positive"
        ),
        # Codex NICE (commit #7 review): a corrupted row with withheld
        # > vested would currently pass. Withheld is always a subset of
        # gross.
        sa.CheckConstraint(
            "shares_withheld <= shares_vested",
            name="ck_rsu_vest_withheld_le_vested",
        ),
        # Idempotency: same vest event re-parsed shouldn't double-insert.
        # (user, grant_id, vest_date) uniquely identifies a vest tranche;
        # even multiple grants vesting on the same day get distinct
        # grant_ids in Schwab's data.
        sa.UniqueConstraint(
            "user_id",
            "grant_id",
            "vest_date",
            name="uq_rsu_vest_events_user_grant_date",
        ),
    )
    op.create_index(
        "ix_rsu_vest_events_user_date",
        "rsu_vest_events",
        ["user_id", "vest_date"],
    )
    op.create_index(
        "ix_rsu_vest_events_grant",
        "rsu_vest_events",
        ["user_id", "grant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rsu_vest_events_grant", table_name="rsu_vest_events"
    )
    op.drop_index(
        "ix_rsu_vest_events_user_date", table_name="rsu_vest_events"
    )
    op.drop_table("rsu_vest_events")
