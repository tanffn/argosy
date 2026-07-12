"""Cadence loop: synthesis stall alerts (Item I reliability).

Every 5 minutes, scan for plan_revision runs that are still ``running``
with no phase heartbeat. Writes monitor flag + inbox row. Does not
require the synthesis process itself to be healthy — only that *some*
backend is alive to tick the scheduler (pair with the backend service
wrapper so a dead uvicorn is restarted).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata
from argosy.services.synthesis_stall_alert import scan_and_alert

_log = get_logger("argosy.loops.synthesis_stall_alert")
_DEFAULT_CRON = "*/5 * * * *"
_DEFAULT_TZ = "Asia/Jerusalem"


def synthesis_stall_alert_metadata() -> JobMetadata:
    return JobMetadata(
        name="synthesis_stall_alert",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Every 5 min Asia/Jerusalem",
        source_kind="monitor",
        description=(
            "Alert when a plan_revision run is in flight with no phase "
            "heartbeat (monitor flag + inbox). Complements the on-demand "
            "liveness reaper."
        ),
        long_running=False,
    )


def _default_session_factory() -> sessionmaker:
    import sqlalchemy as sa

    from argosy.state import db as db_mod

    url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
    engine = sa.create_engine(url, connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine, expire_on_commit=False)


class SynthesisStallAlertLoop(CadenceLoop):
    name = "synthesis_stall_alert"

    def __init__(
        self,
        *,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | None = None,
        schedule: LoopSchedule | None = None,
        alert_minutes: int | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule
            or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._session_factory = session_factory
        self._alert_minutes = alert_minutes

    async def tick(
        self, *, now: Callable[[], datetime] | None = None,
    ) -> dict[str, Any] | None:
        factory = self._session_factory or _default_session_factory()
        sess = factory()
        try:
            clock = now() if now is not None else None
            result = scan_and_alert(
                sess,
                user_id=self.user_id,
                now=clock,
                alert_minutes=self._alert_minutes,
            )
            sess.commit()
            _log.info("synthesis_stall_alert.tick", **result)
            return result
        except Exception as exc:  # noqa: BLE001
            sess.rollback()
            _log.exception("synthesis_stall_alert.tick_failed", error=str(exc)[:200])
            return {"error": str(exc)[:200]}
        finally:
            sess.close()
