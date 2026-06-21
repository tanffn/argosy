"""Add ``status`` lifecycle column to ``monitor_flags``.

Pre-0072 the Home Red-Flag Strip (``GET /api/retirement/monitor/flags``)
filtered active flags on ``acknowledged_at IS NULL`` + expiry alone. Because
the state-observer's dedup_key embeds the (bucket, field-index) — which jitter
run-to-run (``large`` vs ``extreme``, ``allocations[1]`` vs ``allocations[5]``,
``current_pct`` vs ``current_k_usd``) — the SAME logical observation lands under
a fresh dedup_key each run and never supersedes its predecessor. The strip
therefore accumulated stale/duplicate observations (a stale fm-rejected
plan-assumption flag, the cash-overweight observation 5x, etc.).

``status`` (``active`` | ``superseded`` | ``acknowledged``) gives each producer
(state_observer / thesis_monitor / plan-promotion) a supersede primitive: a new
run marks its PRIOR same-producer active rows ``superseded`` and the query
returns only ``active`` rows. The column is backfilled deterministically from
the existing ``acknowledged_at``: acknowledged rows -> ``acknowledged``,
everything else -> ``active`` (the one-time accumulation cleanup is a separate
data step, not this schema migration).

Follows the additive-column precedent (no batch_alter rebuild needed — adding a
NOT NULL column with a server_default is in-place on SQLite).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0072_monitor_flag_status"
down_revision: str | None = "0071_derivation_graph_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("monitor_flags")}
    if "status" in cols:
        return  # idempotent — already applied out-of-band

    op.add_column(
        "monitor_flags",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
    )
    # Backfill from acknowledged_at so the existing UI behaviour
    # (acknowledged rows hidden) is preserved exactly.
    op.execute(
        sa.text(
            "UPDATE monitor_flags SET status = 'acknowledged' "
            "WHERE acknowledged_at IS NOT NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("monitor_flags")}
    if "status" not in cols:
        return
    with op.batch_alter_table("monitor_flags") as batch:
        batch.drop_column("status")
