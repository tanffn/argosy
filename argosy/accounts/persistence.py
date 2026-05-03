"""Argonaut snapshot persistence helpers (Phase 5).

Tiny CRUD around the `argonaut_snapshots` table. Idempotent upsert per
(user_id, account_id, date) so the daily-brief loop can re-run without
double-counting; `get_prior_total_usd` is used to compute the day_pnl_usd
delta.
"""

from __future__ import annotations

from datetime import date as _date_cls

from sqlalchemy import select

from argosy.accounts.argonaut import ArgonautSnapshotPayload
from argosy.state import db as db_mod
from argosy.state.models import ArgonautSnapshot


async def upsert_snapshot(payload: ArgonautSnapshotPayload) -> int:
    """Insert or update one `argonaut_snapshots` row. Returns the row id."""
    async with db_mod.get_session() as session:
        existing = (
            await session.execute(
                select(ArgonautSnapshot).where(
                    ArgonautSnapshot.user_id == payload.user_id,
                    ArgonautSnapshot.account_id == payload.account_id,
                    ArgonautSnapshot.date == payload.date,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            row = ArgonautSnapshot(
                user_id=payload.user_id,
                account_id=payload.account_id,
                date=payload.date,
                total_value_usd=payload.total_value_usd,
                cash_usd=payload.cash_usd,
                positions_value_usd=payload.positions_value_usd,
                day_pnl_usd=payload.day_pnl_usd,
                recorded_at=payload.recorded_at,
            )
            session.add(row)
            await session.commit()
            return int(row.id)

        existing.total_value_usd = payload.total_value_usd
        existing.cash_usd = payload.cash_usd
        existing.positions_value_usd = payload.positions_value_usd
        existing.day_pnl_usd = payload.day_pnl_usd
        existing.recorded_at = payload.recorded_at
        await session.commit()
        return int(existing.id)


async def get_prior_total_usd(
    *, user_id: str, account_id: str, before: _date_cls | None = None
) -> float | None:
    """Return the most-recent prior `total_value_usd` for day_pnl computation.

    None when no prior row exists.
    """
    target_date = (before or _date_cls.today()).isoformat()
    async with db_mod.get_session() as session:
        stmt = (
            select(ArgonautSnapshot)
            .where(
                ArgonautSnapshot.user_id == user_id,
                ArgonautSnapshot.account_id == account_id,
                ArgonautSnapshot.date < target_date,
            )
            .order_by(ArgonautSnapshot.date.desc())
            .limit(1)
        )
        prior = (await session.execute(stmt)).scalar_one_or_none()
        if prior is None:
            return None
        return float(prior.total_value_usd)


async def list_snapshots(
    *, user_id: str, account_id: str | None = None, limit: int = 365
) -> list[ArgonautSnapshot]:
    """Return chronological snapshots, oldest first."""
    async with db_mod.get_session() as session:
        stmt = select(ArgonautSnapshot).where(ArgonautSnapshot.user_id == user_id)
        if account_id is not None:
            stmt = stmt.where(ArgonautSnapshot.account_id == account_id)
        stmt = stmt.order_by(ArgonautSnapshot.date.asc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())


__all__ = ["get_prior_total_usd", "list_snapshots", "upsert_snapshot"]
