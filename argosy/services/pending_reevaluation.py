"""Service layer for the /consult auto-retry queue.

Persists + sweeps rows of ``pending_reevaluations`` per
migration/model 0059. The queue's job: when a /consult lands at
INSUFFICIENT_DATA (trader couldn't complete the analysis because
load-bearing inputs were missing AFTER the per-ticker remediation
flow exhausted its retries), enqueue a row here. A daily job
(``argosy/orchestrator/loops/pending_reevaluation_daily.py``) sweeps
the queue and re-fires each pending consult. On a real
BUY/HOLD/SELL verdict the user is notified via the existing
notification_dispatcher.

The queue is intentionally small + idempotent — ``(user_id, ticker,
consult_mode)`` is the unique key, so a second INSUFFICIENT_DATA
result for the same ticker+mode UPSERTs the existing row
(increments attempt_count, refreshes last_failure_reason). The
soft cap on attempts (default 7) keeps a persistent-bad-data
ticker from looping forever.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from argosy.logging import get_logger
from argosy.state import db as db_mod
from argosy.state.models import PendingReevaluation

log = get_logger(__name__)


#: Soft cap on retry attempts before the row is marked 'abandoned'.
#: When yfinance / Finnhub / SEC EDGAR all consistently return bad
#: data for a ticker, no amount of retry will resolve it — the user
#: needs to be told "we tried N times, this won't auto-resolve, you
#: need to either configure a new data source or accept the gap".
MAX_REEVAL_ATTEMPTS: int = 7


ConsultMode = Literal["tactical_trade", "long_hold"]


async def enqueue_pending_reevaluation(
    *,
    user_id: str,
    ticker: str,
    tier_value: str,
    consult_mode: ConsultMode,
    user_constraints: str,
    failure_reason: str,
) -> int:
    """Enqueue (or UPSERT) a pending re-evaluation row. Returns the row id.

    If a 'pending' row already exists for (user_id, ticker, consult_mode),
    we increment attempt_count + refresh last_failure_reason + bump
    last_attempted_at. If the existing row is 'resolved' or 'abandoned',
    we move it back to 'pending' and reset attempt_count to 1 — the
    user explicitly tried again, give them a fresh retry budget.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with db_mod.get_session() as session:
        existing = (await session.execute(
            select(PendingReevaluation).where(
                PendingReevaluation.user_id == user_id,
                PendingReevaluation.ticker == ticker,
                PendingReevaluation.consult_mode == consult_mode,
            )
        )).scalar_one_or_none()

        if existing is None:
            row = PendingReevaluation(
                user_id=user_id,
                ticker=ticker,
                tier_value=tier_value,
                consult_mode=consult_mode,
                user_constraints=user_constraints,
                last_failure_reason=failure_reason,
                status="pending",
                attempt_count=1,
                created_at=now,
                last_attempted_at=now,
            )
            session.add(row)
            await session.flush()
            row_id = row.id
            await session.commit()
            log.info(
                "pending_reevaluation.enqueued",
                user_id=user_id, ticker=ticker, consult_mode=consult_mode,
                row_id=row_id,
            )
            return row_id

        if existing.status == "pending":
            existing.attempt_count += 1
            existing.last_failure_reason = failure_reason
            existing.last_attempted_at = now
            await session.commit()
            log.info(
                "pending_reevaluation.attempt_recorded",
                user_id=user_id, ticker=ticker, consult_mode=consult_mode,
                row_id=existing.id, attempt_count=existing.attempt_count,
            )
            return existing.id

        # 'resolved' or 'abandoned' — user is trying again. Reset.
        # Codex nit fix: capture prior_status BEFORE mutating so the
        # log entry records what the row was, not what it just became.
        prior_status = existing.status
        existing.status = "pending"
        existing.attempt_count = 1
        existing.last_failure_reason = failure_reason
        existing.last_attempted_at = now
        existing.resolved_decision_run_id = None
        await session.commit()
        log.info(
            "pending_reevaluation.reopened",
            user_id=user_id, ticker=ticker, consult_mode=consult_mode,
            row_id=existing.id, prior_status=prior_status,
        )
        return existing.id


async def list_pending_for_sweep(
    *, user_id: str | None = None,
) -> list[PendingReevaluation]:
    """Return all pending rows (optionally filtered by user_id), ordered
    by oldest-attempt first so a backlog gets fair attention."""
    async with db_mod.get_session() as session:
        stmt = select(PendingReevaluation).where(
            PendingReevaluation.status == "pending",
        )
        if user_id is not None:
            stmt = stmt.where(PendingReevaluation.user_id == user_id)
        stmt = stmt.order_by(PendingReevaluation.last_attempted_at.asc())
        rows = (await session.execute(stmt)).scalars().all()
        return list(rows)


async def mark_resolved(
    *, row_id: int, decision_run_id: int,
) -> None:
    """The retry succeeded — mark the row resolved + link the new run.
    The daily job calls this after a successful re-evaluation; the
    notification dispatcher then deep-links the user to ``/decisions/``
    + ``decision_run_id``."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with db_mod.get_session() as session:
        await session.execute(
            update(PendingReevaluation)
            .where(PendingReevaluation.id == row_id)
            .values(
                status="resolved",
                resolved_decision_run_id=decision_run_id,
                last_attempted_at=now,
            )
        )
        await session.commit()
    log.info(
        "pending_reevaluation.resolved",
        row_id=row_id, decision_run_id=decision_run_id,
    )


async def mark_abandoned(*, row_id: int, final_reason: str) -> None:
    """Soft cap reached — give up retrying. The daily job will dispatch
    a notification telling the user "tried N times, the data didn't
    clean up — manual intervention needed"."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with db_mod.get_session() as session:
        await session.execute(
            update(PendingReevaluation)
            .where(PendingReevaluation.id == row_id)
            .values(
                status="abandoned",
                last_failure_reason=final_reason,
                last_attempted_at=now,
            )
        )
        await session.commit()
    log.info(
        "pending_reevaluation.abandoned",
        row_id=row_id, final_reason=final_reason[:200],
    )


async def record_attempt(
    *, row_id: int, failure_reason: str,
) -> int:
    """Increment attempt_count + refresh last_failure_reason. Returns
    the new attempt_count so the caller can decide to mark_abandoned."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with db_mod.get_session() as session:
        row = await session.get(PendingReevaluation, row_id)
        if row is None:
            return 0
        row.attempt_count += 1
        row.last_failure_reason = failure_reason
        row.last_attempted_at = now
        await session.commit()
        return row.attempt_count


__all__ = [
    "MAX_REEVAL_ATTEMPTS",
    "enqueue_pending_reevaluation",
    "list_pending_for_sweep",
    "mark_abandoned",
    "mark_resolved",
    "record_attempt",
]
