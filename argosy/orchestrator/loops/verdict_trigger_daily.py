"""Daily deterministic verdict-trigger checker (Item B).

Cheap cron — price thresholds vs daily quotes, dated events vs calendar.
A fired trigger UNLOCKS re-evaluation (needs-confirm inbox row); it never
launches the fleet. Schedule: 07:00 Asia/Jerusalem (after overnight quotes
settle, before the 15:30 signal streams / 16:00 discovery funnel).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata
from argosy.services.verdict_registry import (
    evaluate_triggers,
    write_unlock_inbox_rows,
)

_log = get_logger("argosy.loops.verdict_trigger_daily")
_DEFAULT_CRON = "0 7 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"


def verdict_trigger_daily_metadata() -> JobMetadata:
    return JobMetadata(
        name="verdict_trigger_daily",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 07:00 Asia/Jerusalem",
        source_kind="monitor",
        description=(
            "Deterministic settled-verdict trigger checker: price_below/above "
            "vs quotes, dated_event vs calendar. Fired triggers write a "
            "needs-confirm unlock inbox row — never spawn agents."
        ),
        long_running=False,
    )


def _default_session_factory() -> sessionmaker:
    from argosy.state import db as db_mod

    url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
    engine = db_mod.create_sync_engine(url)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _fetch_quotes_for_subjects(
    session: Session, *, user_id: str, subjects: list[str]
) -> dict[str, float]:
    """Fresh cache/Yahoo prices; ``user_id`` remains for seam compatibility."""
    del user_id
    from argosy.services.verdict_trigger_quotes import fetch_trigger_quotes

    return fetch_trigger_quotes(session, subjects)


class VerdictTriggerDailyLoop(CadenceLoop):
    """Sweep settled verdicts; unlock inbox rows for fired triggers."""

    name = "verdict_trigger_daily"

    def __init__(
        self,
        *,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | None = None,
        quotes_fn: Callable[[Session, str, list[str]], dict[str, float]] | None = None,
        today: date | None = None,
        schedule: LoopSchedule | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule
            or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._session_factory = session_factory
        self._quotes_fn = quotes_fn or _fetch_quotes_for_subjects
        self._today = today

    async def tick(
        self, *, now: Callable[[], datetime] | None = None
    ) -> dict[str, Any]:
        # Base-class contract (base.py::CadenceLoop.tick): the scheduler
        # passes its clock as ``now=``. A constructor-injected ``today``
        # is an explicit pin (tests) and wins; otherwise the scheduler
        # clock supplies the as-of date for dated triggers.
        today = self._today if self._today is not None else (
            now().date() if now is not None else None
        )
        # A synchronous SQLite wait on the event loop can prevent another
        # async writer from committing and releasing the very lock we need.
        # Keep the ENTIRE session lifecycle on one worker, not just quotes.
        worker = asyncio.create_task(asyncio.to_thread(self._tick_sync, today))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Do not release the scheduler's job lock while a worker can still
            # write. Python cannot cancel an already-running thread. Shutdown
            # can cancel again after its grace timeout: shield EVERY drain wait.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                try:
                    worker.result()
                except Exception:
                    _log.exception("verdict_trigger.cancelled_worker_failed")
            raise

    def _tick_sync(self, today: date | None) -> dict[str, Any]:
        factory = self._session_factory or _default_session_factory()
        sess = factory()
        try:
            from sqlalchemy import select

            from argosy.state.models import Verdict

            subjects = list(
                sess.execute(
                    select(Verdict.subject).where(
                        Verdict.user_id == self.user_id,
                        Verdict.settled.is_(True),
                    )
                ).scalars().all()
            )
            # The injectable contract is positional (session, user_id,
            # subjects) — tests rely on it; the module default takes
            # keyword-only args. Bridge here so BOTH work (live-smoke
            # 2026-07-12: the default path had never been exercised).
            if self._quotes_fn is _fetch_quotes_for_subjects:
                quotes = _fetch_quotes_for_subjects(
                    sess, user_id=self.user_id, subjects=subjects,
                )
            else:
                quotes = self._quotes_fn(sess, self.user_id, subjects)
            # Quotes are plain values. End the old read snapshot before
            # acquiring a short write transaction; never hold it over network
            # I/O. Re-read current verdicts and dedup rows under that lock.
            sess.rollback()
            if sess.get_bind().dialect.name == "sqlite":
                from sqlalchemy import text

                sess.execute(text("BEGIN IMMEDIATE"))
            fired = evaluate_triggers(
                sess,
                user_id=self.user_id,
                quotes=quotes,
                today=today,
            )
            ids = write_unlock_inbox_rows(
                sess, user_id=self.user_id, fired=fired,
            )
            sess.commit()
            _log.info(
                "verdict_trigger.tick",
                subjects=len(subjects),
                quotes=len(quotes),
                fired=len(fired),
                unlock_rows=len(ids),
            )
            return {
                "subjects": len(subjects),
                "quotes": len(quotes),
                "fired": len(fired),
                "unlock_proposal_ids": ids,
            }
        except Exception as exc:  # noqa: BLE001
            sess.rollback()
            _log.exception("verdict_trigger.tick_failed", error=str(exc)[:200])
            return {"error": str(exc)[:200]}
        finally:
            sess.close()
            if self._session_factory is None:
                factory.kw["bind"].dispose()
