"""``SnapshotRefreshJob`` — reprice the latest portfolio snapshot with live quotes.

The TSV is an Argosy OUTPUT, never an input dependency: when holdings have not
changed, a fresh portfolio picture is the old snapshot's quantities re-priced
(see :mod:`argosy.services.snapshot_refresh`). Registered ``enabled=True``
(Ariel's 2026-07-08 go); manual ``Run now`` (``fire_now``) uses the same tick.

Same-code-path contract: the cron cadence and the manual trigger both go
through :meth:`tick`. Quote/FX fetches are blocking network calls that use
``run_coro_sync`` internally, so the work runs via ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from argosy.adapters.data.async_bridge import mismatch_count
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata
from argosy.services.snapshot_refresh import refresh_portfolio_snapshot

_log = get_logger("argosy.jobs.snapshot_refresh")

_DEFAULT_CRON = "0 8 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"

_SESSION_FACTORY: tuple[str, sessionmaker] | None = None


def _build_default_session_factory() -> sessionmaker:
    """Cached sync ``sessionmaker`` bound to the configured DB (rebuilds if the
    db_file changes). Mirrors ``holdings_review`` — the refresh service needs a
    sync Session."""
    global _SESSION_FACTORY
    import sqlalchemy as sa

    from argosy.config import get_settings

    db_file = str(get_settings().db_file)
    if _SESSION_FACTORY is not None and _SESSION_FACTORY[0] == db_file:
        return _SESSION_FACTORY[1]
    engine = sa.create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _SESSION_FACTORY = (db_file, factory)
    return factory


def snapshot_refresh_metadata() -> JobMetadata:
    return JobMetadata(
        name="snapshot_refresh",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 08:00 Asia/Jerusalem",
        source_kind="ingest",
        description=(
            "Self-refresh the portfolio snapshot: carry quantities from the "
            "latest snapshot, re-price every priceable position with live "
            "quotes + fresh FX, and insert a new provenance-marked snapshot "
            "row. Cash / pensions / unpriceable rows carry over; a quote miss "
            "carries the old value and is recorded as reprice_miss:<symbol>."
        ),
    )


class SnapshotRefreshJob(CadenceLoop):
    """Reprice-the-book loop. Quantities never change; only prices/FX do."""

    name = "snapshot_refresh"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = False,
        user_id: str = "ariel",
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        refresh_fn: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._session_factory = session_factory
        self._refresh_fn = refresh_fn or refresh_portfolio_snapshot
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict | None:
        self.last_output_summary = None
        run_at = (now or (lambda: datetime.now(timezone.utc)))()
        _log.info("snapshot_refresh.tick.start", run_at=run_at.isoformat())

        def _work() -> dict[str, Any]:
            factory = self._session_factory or _build_default_session_factory()
            session = factory()
            try:
                res = self._refresh_fn(session, user_id=self.user_id)
            finally:
                session.close()
            return res.summary() if hasattr(res, "summary") else dict(res or {})

        before_infra = mismatch_count()
        summary = await asyncio.to_thread(_work)
        delta = mismatch_count() - before_infra
        if delta > 0:
            summary = dict(summary)
            summary["infra_degraded"] = True
            summary["infra_data_failures"] = delta
            # Back-compat alias used by earlier Stream E summaries.
            summary["event_loop_mismatches"] = delta
            _log.error(
                "snapshot_refresh.infra_degraded",
                infra_data_failures=delta,
                repriced=summary.get("repriced"),
                carried=summary.get("carried"),
            )
        self.last_output_summary = summary
        _log.info("snapshot_refresh.tick.done", **summary)
        return summary


__all__ = ["SnapshotRefreshJob", "snapshot_refresh_metadata"]
