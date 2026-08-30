"""Daily primary SEC earnings-filing coverage monitor."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.news_daily import resolve_holdings_split
from argosy.services.jobs.registry import JobMetadata
from argosy.services.sec_earnings_monitor import (
    SecEarningsCoverageSummary,
    run_sec_earnings_checks,
)

_DEFAULT_CRON = "40 16 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"


def sec_earnings_daily_metadata() -> JobMetadata:
    return JobMetadata(
        name="sec_earnings_daily",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 16:40 Asia/Jerusalem",
        source_kind="monitor",
        description=(
            "Checks primary SEC results filings for every held single stock, "
            "records per-ticker receipts, and emits recent filings into the "
            "news-analysis path."
        ),
        long_running=False,
    )


def _default_session_factory() -> sessionmaker:
    from argosy.config import get_settings
    from argosy.state.db import create_sync_engine

    url = str(get_settings().database_url).replace("+aiosqlite", "")
    return sessionmaker(bind=create_sync_engine(url), expire_on_commit=False)


class SecEarningsDailyJob(CadenceLoop):
    name = "sec_earnings_daily"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: Callable[[], Session] | None = None,
        runner: Callable[..., SecEarningsCoverageSummary] = run_sec_earnings_checks,
        holdings_resolver=resolve_holdings_split,
    ) -> None:
        super().__init__(
            schedule=schedule or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._session_factory = session_factory
        self._runner = runner
        self._holdings_resolver = holdings_resolver

    def _run_sync(self, checked_at: datetime) -> dict:
        factory = self._session_factory or _default_session_factory()
        session = factory()
        try:
            holdings = self._holdings_resolver(session, self.user_id)
            summary = self._runner(
                session,
                user_id=self.user_id,
                tickers=holdings.single_stocks,
                checked_at=checked_at,
            )
            session.commit()
            return summary.to_dict()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict:
        checked_at = (now or (lambda: datetime.now(UTC)))()
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        return await asyncio.to_thread(self._run_sync, checked_at)


__all__ = ["SecEarningsDailyJob", "sec_earnings_daily_metadata"]
