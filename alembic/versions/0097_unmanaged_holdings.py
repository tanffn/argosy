"""unmanaged_holdings — durable deliberately-excluded positions + policy + backfill.

NVDA (and any future unmanaged holding) must remain PRESENT for total-book
arithmetic (estate / net worth / FI shock / FX / tax) while being excluded
from sleeve-allocation percentage math. Absence from a TSV re-ingest must
not silently erase an unmanaged position from the total book.

Schema supports per-(user, symbol, location) rows so multi-account lots
survive. A data backfill seeds active rows from the most recent historical
snapshot that still carried each policy symbol — so an empty table never
quietly understates estate exposure after upgrade.

Revision ID: 0097_unmanaged_holdings
Revises: 0094_expense_tag_rules
"""
from __future__ import annotations

import json
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0097_unmanaged_holdings"
down_revision: str | None = "0094_expense_tag_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Seed policy for every existing user; the family deliberately manages NVDA
# outside the sleeve book. Data-driven thereafter (rows in unmanaged_symbol_policy).
_DEFAULT_POLICY_SYMBOLS = ("NVDA",)


def upgrade() -> None:
    op.create_table(
        "unmanaged_symbol_policy",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "user_id", "symbol", name="uq_unmanaged_symbol_policy_user_symbol"
        ),
    )
    op.create_index(
        "ix_unmanaged_symbol_policy_user",
        "unmanaged_symbol_policy",
        ["user_id"],
    )

    op.create_table(
        "unmanaged_holdings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        # Per-account / per-lot identity — Schwab vs elsewhere must not collapse.
        sa.Column("location", sa.String(128), nullable=False, server_default=""),
        sa.Column("shares", sa.Float(), nullable=True),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("usd_value_k", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("asset_type", sa.String(128), nullable=False, server_default=""),
        sa.Column("details", sa.String(256), nullable=False, server_default=""),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=False,
            server_default="excluded_from_sleeve_math",
        ),
        # active | retired — retirement is the lifecycle end (genuine sale).
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "retired_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # Last stored mark date (informational). Live money uses reprice.
        sa.Column("valued_as_of", sa.Date(), nullable=True),
        # When the share count was last confirmed — quantity trust horizon.
        sa.Column("observed_as_of", sa.Date(), nullable=True),
        sa.UniqueConstraint(
            "user_id",
            "symbol",
            "location",
            name="uq_unmanaged_holdings_user_symbol_location",
        ),
    )
    op.create_index(
        "ix_unmanaged_holdings_user",
        "unmanaged_holdings",
        ["user_id"],
    )
    op.create_index(
        "ix_unmanaged_holdings_user_status",
        "unmanaged_holdings",
        ["user_id", "status"],
    )

    _seed_policy_and_backfill()


def _seed_policy_and_backfill() -> None:
    """Idempotent: policy rows for every user + holdings from last NVDA-bearing snap."""
    bind = op.get_bind()
    users = [r[0] for r in bind.execute(sa.text("SELECT id FROM users")).fetchall()]
    for uid in users:
        for sym in _DEFAULT_POLICY_SYMBOLS:
            bind.execute(
                sa.text(
                    "INSERT OR IGNORE INTO unmanaged_symbol_policy (user_id, symbol) "
                    "VALUES (:u, :s)"
                ),
                {"u": uid, "s": sym},
            )

    # Backfill active holdings from the newest snapshot (by id) that still
    # carries each policy symbol. Never invents quantities — only copies
    # observed historical rows. Persists observed_as_of = valued_as_of =
    # snapshot_date so quantity trust is dated and price is never treated
    # as current money without a live reprice.
    snaps = bind.execute(
        sa.text(
            "SELECT id, user_id, positions_json, snapshot_date "
            "FROM portfolio_snapshots ORDER BY id DESC"
        )
    ).fetchall()
    # Per (user, symbol, location): keep the first (newest) observation.
    seen: set[tuple[str, str, str]] = set()
    for _sid, user_id, positions_json, snap_date in snaps:
        try:
            positions = json.loads(positions_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(positions, list):
            continue
        for p in positions:
            if not isinstance(p, dict):
                continue
            sym = str(p.get("symbol") or "").strip().upper()
            if sym not in _DEFAULT_POLICY_SYMBOLS:
                continue
            loc = str(p.get("location") or "").strip()
            key = (str(user_id), sym, loc)
            if key in seen:
                continue
            seen.add(key)
            bind.execute(
                sa.text(
                    "INSERT OR IGNORE INTO unmanaged_holdings "
                    "(user_id, symbol, location, shares, current_price, usd_value_k, "
                    "currency, asset_type, details, reason, status, valued_as_of, "
                    "observed_as_of) "
                    "VALUES (:u, :s, :loc, :sh, :px, :vk, :cur, :at, :det, "
                    ":reason, 'active', :vas, :oas)"
                ),
                {
                    "u": user_id,
                    "s": sym,
                    "loc": loc,
                    "sh": p.get("shares"),
                    "px": p.get("current_price"),
                    "vk": p.get("usd_value_k"),
                    "cur": str(p.get("currency") or "USD"),
                    "at": str(p.get("asset_type") or ""),
                    "det": str(p.get("details") or ""),
                    "reason": "backfill_from_historical_snapshot",
                    "vas": snap_date,
                    "oas": snap_date,
                },
            )


def downgrade() -> None:
    op.drop_index("ix_unmanaged_holdings_user_status", table_name="unmanaged_holdings")
    op.drop_index("ix_unmanaged_holdings_user", table_name="unmanaged_holdings")
    op.drop_table("unmanaged_holdings")
    op.drop_index(
        "ix_unmanaged_symbol_policy_user", table_name="unmanaged_symbol_policy"
    )
    op.drop_table("unmanaged_symbol_policy")
