"""Cross-cutting read-only query helpers.

Pure functions that wrap the SQLAlchemy boilerplate for the few
non-trivial joins that several callers need. Keep these helpers
small + obvious: anything with non-trivial business logic belongs in
its owning module (the agent, the loop, the route), not here.

Currently houses:

  - ``get_user_pension_snapshots(user_id)`` — returns the most recent
    pension-fund snapshot per `(user_id, fund_id)` tuple. Used by
    `TaxAnalystAgent` callers to inject pension performance context
    into the tax-analyst prompt without coupling the agent module to
    the ORM layer.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from argosy.state import db as db_mod
from argosy.state.models import PensionFundSnapshot


def _row_to_dict(row: PensionFundSnapshot) -> dict[str, Any]:
    """Render one snapshot ORM row as a plain JSON-safe dict."""
    return {
        "id": row.id,
        "user_id": row.user_id,
        "fund_id": row.fund_id,
        "fund_name": row.fund_name,
        "fund_type": row.fund_type,
        "manager": row.manager,
        "return_pct_12m": (
            float(row.return_pct_12m) if row.return_pct_12m is not None else None
        ),
        "benchmark_return_pct_12m": (
            float(row.benchmark_return_pct_12m)
            if row.benchmark_return_pct_12m is not None
            else None
        ),
        "relative_to_benchmark_pct": (
            float(row.relative_to_benchmark_pct)
            if row.relative_to_benchmark_pct is not None
            else None
        ),
        "balance_nis": (
            float(row.balance_nis) if row.balance_nis is not None else None
        ),
        "snapshot_at": (
            row.snapshot_at.isoformat() if row.snapshot_at else None
        ),
        "source_url": row.source_url,
    }


async def get_user_pension_snapshots(
    user_id: str,
    *,
    only_latest_per_fund: bool = True,
) -> list[dict[str, Any]]:
    """Return pension snapshots for ``user_id`` as plain dicts.

    Args:
        user_id: the user. Cross-user isolation is enforced at the SQL
            level — every code path filters by ``user_id`` before
            touching ``snapshot_at``.
        only_latest_per_fund: when True (default) return only the
            most-recent snapshot per ``fund_id``. When False return
            the entire history ordered by ``snapshot_at`` descending.

    Implementation note: the ``only_latest_per_fund=True`` path uses a
    ``ROW_NUMBER() OVER (PARTITION BY fund_id ORDER BY snapshot_at DESC)``
    window function so the per-fund-latest filter happens in SQL rather
    than fetch-then-Python. SQLite ≥ 3.25 supports window functions and
    Argosy targets 3.40+, so no fallback is needed.

    Returned dicts mirror the column names on ``PensionFundSnapshot``,
    plus ``snapshot_at`` rendered as ISO 8601 for safe JSON
    serialization.
    """
    async with db_mod.get_session() as session:
        if only_latest_per_fund:
            # Window-function path — ROW_NUMBER() over user-scoped rows.
            # The WHERE filters by user_id BEFORE the partition runs, so
            # rn=1 always identifies the user's own most-recent row per
            # fund; there is zero cross-user leakage even when the same
            # fund_id appears for multiple users.
            #
            # Strategy: build a subquery that selects only (id, rn) for
            # the user's rows, then join the full ORM-mapped table on id
            # WHERE rn=1. Pulling just the id from the subquery keeps the
            # outer SELECT clean of duplicate column names.
            rn = (
                func.row_number()
                .over(
                    partition_by=PensionFundSnapshot.fund_id,
                    order_by=PensionFundSnapshot.snapshot_at.desc(),
                )
                .label("rn")
            )
            ranked = (
                select(PensionFundSnapshot.id.label("id"), rn)
                .where(PensionFundSnapshot.user_id == user_id)
                .subquery()
            )
            # Belt-and-braces: also constrain the OUTER select on user_id.
            # The join via the user-scoped subquery already limits this to
            # the user's rows, but a redundant WHERE keeps cross-user
            # isolation visible at the top of the statement and would
            # survive any future refactor that loosens the subquery.
            stmt = (
                select(PensionFundSnapshot)
                .join(ranked, PensionFundSnapshot.id == ranked.c.id)
                .where(PensionFundSnapshot.user_id == user_id)
                .where(ranked.c.rn == 1)
                .order_by(PensionFundSnapshot.snapshot_at.desc())
            )
            result = await session.execute(stmt)
        else:
            result = await session.execute(
                select(PensionFundSnapshot)
                .where(PensionFundSnapshot.user_id == user_id)
                .order_by(PensionFundSnapshot.snapshot_at.desc())
            )
        rows = result.scalars().all()

    return [_row_to_dict(row) for row in rows]


__all__ = ["get_user_pension_snapshots"]
