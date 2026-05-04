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

from sqlalchemy import select

from argosy.state import db as db_mod
from argosy.state.models import PensionFundSnapshot


async def get_user_pension_snapshots(
    user_id: str,
    *,
    only_latest_per_fund: bool = True,
) -> list[dict[str, Any]]:
    """Return pension snapshots for ``user_id`` as plain dicts.

    Args:
        user_id: the user.
        only_latest_per_fund: when True (default) return only the
            most-recent snapshot per `(user_id, fund_id)` pair. When
            False return the entire history ordered by `snapshot_at`
            descending.

    Returned dicts mirror the column names on `PensionFundSnapshot`,
    plus `snapshot_at` rendered as ISO 8601 for safe JSON
    serialization.
    """
    async with db_mod.get_session() as session:
        result = await session.execute(
            select(PensionFundSnapshot)
            .where(PensionFundSnapshot.user_id == user_id)
            .order_by(PensionFundSnapshot.snapshot_at.desc())
        )
        rows = result.scalars().all()

    out: list[dict[str, Any]] = []
    seen_funds: set[str] = set()
    for row in rows:
        if only_latest_per_fund:
            if row.fund_id in seen_funds:
                continue
            seen_funds.add(row.fund_id)
        out.append(
            {
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
        )
    return out


__all__ = ["get_user_pension_snapshots"]
