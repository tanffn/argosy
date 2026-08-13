"""instrument_classification — ticker → sector_code table for sector-cap preflight.

Phase 3 adds a sector-cap check to risk_preflight so a buy that would push the
information-technology sector beyond 35% is blocked, not just the per-ticker
single-name cap.  The check needs a ticker → sector mapping that is:
  * deterministic (no live vendor call at preflight time)
  * auditable (DB row, not only in-process memory)
  * curated (same authority as instrument_reference.py)

This migration creates the ``instrument_classification`` table and seeds it
from the curated ``argosy.services.instrument_reference._REFERENCE`` dict —
the existing single source of truth for every instrument currently in the book.
Only tickers with a non-None InstrumentRef are seeded (all of them, since the
reference only carries known instruments).

Revision ID: 0103_instrument_classification
Revises: 0102_gate_outcomes
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0103_instrument_classification"
down_revision: str | None = "0102_gate_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instrument_classification",
        sa.Column("ticker", sa.String(32), primary_key=True),
        sa.Column("sector_code", sa.String(64), nullable=False),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            server_default="instrument_reference",
        ),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confidence", sa.String(8), nullable=True, server_default="high"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # Seed from the curated in-code reference table.  Import here so the data
    # stays in sync with the Python module — there is no second source.
    try:
        from argosy.services.instrument_reference import _REFERENCE  # type: ignore[attr-defined]
    except Exception:
        # If the module is not importable (CI dry-run, bare alembic env),
        # skip the seed gracefully — the table is left empty and must be
        # populated manually before the sector-cap check can fire.
        return

    now = datetime.now(timezone.utc).isoformat()
    conn = op.get_bind()
    for ticker, ref in _REFERENCE.items():
        conn.execute(
            sa.text(
                "INSERT OR IGNORE INTO instrument_classification "
                "(ticker, sector_code, source, as_of, confidence, updated_at) "
                "VALUES (:ticker, :sector_code, :source, :as_of, :confidence, :updated_at)"
            ),
            {
                "ticker": ticker,
                "sector_code": ref.sector,
                "source": "instrument_reference",
                "as_of": now,
                "confidence": "high",
                "updated_at": now,
            },
        )


def downgrade() -> None:
    op.drop_table("instrument_classification")
