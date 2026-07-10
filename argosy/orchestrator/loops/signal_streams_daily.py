"""Daily early-signal ingest, before the 16:00 discovery funnel."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata
from argosy.services.signal_streams.base import SignalStream
from argosy.services.signal_streams.contracts import (
    GovContractsConfig,
    GovContractsStream,
)
from argosy.services.signal_streams.pipeline import process_nominations
from argosy.state.models import Prediction

_log = get_logger("argosy.loops.signal_streams_daily")
_DEFAULT_CRON = "30 15 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"


def signal_streams_daily_metadata() -> JobMetadata:
    return JobMetadata(
        name="signal_streams_daily",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 15:30 Asia/Jerusalem",
        source_kind="ingest",
        description=(
            "Fetches configured early-signal streams with per-stream failure "
            "isolation, writes prediction-ledger rows, and nominates liquid "
            "names into the 16:00 discovery funnel."
        ),
        long_running=False,
    )


def _default_session_factory() -> sessionmaker:
    import sqlalchemy as sa

    from argosy.state import db as db_mod

    url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
    engine = sa.create_engine(
        url, connect_args={"check_same_thread": False}
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def _default_streams(user_id: str) -> list[SignalStream]:
    from argosy.config import load_signal_streams_config

    cfg = load_signal_streams_config(user_id)
    if not cfg.enabled:
        return []
    gov = cfg.gov_contracts
    return [
        GovContractsStream(
            config=GovContractsConfig(
                materiality_threshold=gov.materiality_threshold,
                lookback_days=gov.lookback_days,
                recent_scan_days=gov.recent_scan_days,
                max_pages_per_query=gov.max_pages_per_query,
            )
        )
    ]


class SignalStreamsDailyLoop(CadenceLoop):
    name = "signal_streams_daily"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        streams: Sequence[SignalStream] | None = None,
        session_factory: sessionmaker | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule
            or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._streams = list(streams) if streams is not None else None
        self._session_factory = session_factory

    def _run_sync(self, now_dt: datetime) -> dict[str, Any]:
        streams = (
            self._streams
            if self._streams is not None
            else _default_streams(self.user_id)
        )
        factory = self._session_factory or _default_session_factory()
        summary: dict[str, Any] = {"streams": {}}
        for stream in streams:
            name = str(getattr(stream, "name", type(stream).__name__))
            session: Session = factory()
            try:
                lookback = int(
                    getattr(getattr(stream, "config", None), "lookback_days", 1)
                )
                recent = int(
                    getattr(
                        getattr(stream, "config", None),
                        "recent_scan_days",
                        lookback,
                    )
                )
                prior_prediction = (
                    session.query(Prediction.id)
                    .filter(
                        Prediction.user_id == self.user_id,
                        Prediction.source == f"signal_stream:{name}",
                    )
                    .first()
                )
                since_days = recent if prior_prediction is not None else lookback
                since: date = now_dt.date() - timedelta(
                    days=max(0, since_days - 1)
                )
                nominations = stream.fetch(session, since=since)
                processed = process_nominations(
                    session,
                    user_id=self.user_id,
                    nominations=nominations,
                    observed_at=now_dt,
                )
                session.commit()
                summary["streams"][name] = {
                    "status": "ok",
                    "nominations": len(nominations),
                    **processed.to_dict(),
                }
            except Exception as exc:  # noqa: BLE001 - stream isolation
                session.rollback()
                summary["streams"][name] = {
                    "status": "error",
                    "error": str(exc)[:300],
                }
                _log.warning(
                    "signal_streams_daily.stream_failed",
                    stream=name,
                    error=str(exc)[:300],
                )
            finally:
                session.close()
        return summary

    async def tick(
        self, *, now: Callable[[], datetime] | None = None
    ) -> dict[str, Any]:
        now_dt = (now or (lambda: datetime.now(UTC)))()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        return await asyncio.to_thread(self._run_sync, now_dt)


__all__ = [
    "SignalStreamsDailyLoop",
    "signal_streams_daily_metadata",
]
