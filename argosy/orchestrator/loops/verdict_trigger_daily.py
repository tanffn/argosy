"""Daily deterministic verdict-trigger checker (Item B).

Cheap cron — price thresholds vs daily quotes, dated events vs calendar.
A fired trigger UNLOCKS re-evaluation (needs-confirm inbox row); it never
launches the fleet. Schedule: 07:00 Asia/Jerusalem (after overnight quotes
settle, before the 15:30 signal streams / 16:00 discovery funnel).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
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
    import sqlalchemy as sa

    from argosy.state import db as db_mod

    url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
    engine = sa.create_engine(url, connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine, expire_on_commit=False)


def _fetch_quotes_for_subjects(
    session: Session, *, user_id: str, subjects: list[str]
) -> dict[str, float]:
    """Best-effort last prices from kv_cache / latest snapshot — never raises."""
    quotes: dict[str, float] = {}
    if not subjects:
        return quotes
    try:
        from sqlalchemy import text

        # Prefer kv_cache quote keys written by price feeds: quote:{SYMBOL}
        for sym in subjects:
            row = session.execute(
                text(
                    "SELECT value FROM kv_cache WHERE key = :k "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"k": f"quote:{sym}"},
            ).scalar()
            if row is None:
                continue
            try:
                import json

                payload = json.loads(row) if isinstance(row, str) else row
                px = (
                    payload.get("price")
                    or payload.get("last")
                    or payload.get("close")
                    if isinstance(payload, dict)
                    else None
                )
                if px is not None:
                    quotes[sym] = float(px)
            except (TypeError, ValueError, AttributeError):
                continue
    except Exception as exc:  # noqa: BLE001
        _log.warning("verdict_trigger.quote_fetch_failed", error=str(exc)[:200])
    return quotes


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

    async def tick(self) -> dict[str, Any]:
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
            quotes = self._quotes_fn(sess, self.user_id, subjects)
            fired = evaluate_triggers(
                sess,
                user_id=self.user_id,
                quotes=quotes,
                today=self._today,
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
