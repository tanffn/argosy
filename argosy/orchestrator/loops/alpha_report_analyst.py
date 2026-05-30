"""``AlphaReportAnalystLoop`` — daily Discord alpha-report analyst cron.

Wires :func:`argosy.services.alpha_report_analyst_runner.run_pending_batch`
into a :class:`CadenceLoop`. Fires at 18:00 IDT daily — one hour after
the news pipeline at 17:00 IDT so all NewsSignal rows from the day's
Discord posts have been ingested before the analyst classifies them.

Schedule rationale
==================

* 17:00 IDT — ``news_daily`` (ingest + news_signal_analyst Stage 2)
  + ``state_observer`` (snapshot + diff + flagger).
* **18:00 IDT** — alpha_report_analyst (this loop). Consumes
  long-form Discord NewsSignals written in the prior hour; the
  state_observer's next-day 17:00 tick reads any
  ``alpha_report_caution`` flags + macro_tone summaries the analyst
  promoted as ONE input among many (no hardcoded "N reports → flag"
  detector per ``feedback_emergent_anomaly_detection``).

Tick body is short — the LLM call is the expensive operation but it
fits comfortably under the scheduler's per-tick budget (one Opus call
per unanalyzed signal; the daily volume is single-digit reports).
The ``limit=20`` cap on ``run_pending_batch`` bounds wall-clock under
a pathological "backlog of 200 unanalyzed signals" case — the next
day's tick drains the rest.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata

_log = get_logger("argosy.loops.alpha_report_analyst")


# Default cron + tz — kept in sync with
# ``cadences.alpha_report_analyst`` in ``argosy/agent_settings.py``.
# 18:00 IDT = one hour after the news pipeline at 17:00.
_DEFAULT_CRON = "0 18 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"

# Per-tick cap on signals processed. Bounded wall-clock for the
# scheduler; a backlog drains over multiple ticks.
DEFAULT_BATCH_LIMIT: int = 20


def alpha_report_analyst_metadata() -> JobMetadata:
    """JobMetadata for admin-UI surfacing.

    ``source_kind='analyst'`` — the analyst is a content-classification
    agent (same family as ``news_signal_analyst``), not a monitor /
    maintenance loop.
    """
    return JobMetadata(
        name="alpha_report_analyst_daily",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 18:00 IDT",
        source_kind="analyst",
        description=(
            "Daily Discord long-form alpha-report analyst. Finds "
            "NewsSignal rows from Discord with raw_text > 500 chars "
            "that lack an alpha_report_analyses row, runs the Opus "
            "alpha_report_analyst on each, persists the analysis, "
            "fans out per-ticker + structural predictions into the "
            "ledger, and promotes severity-hinting cautions to "
            "alpha_report_caution monitor flags. Batch limit per tick: "
            f"{DEFAULT_BATCH_LIMIT}."
        ),
        long_running=False,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Module-level cached session factory — same pattern as state_observer
# / news_daily loops. Rebuilds when ``db_file`` changes (test reloads).
_DEFAULT_SESSION_FACTORY: tuple[str, sessionmaker] | None = None


def _build_default_session_factory() -> sessionmaker:
    """Return a cached sync sessionmaker bound to the configured DB."""
    global _DEFAULT_SESSION_FACTORY

    import sqlalchemy as sa

    from argosy.config import get_settings

    settings = get_settings()
    db_file = str(settings.db_file)

    if _DEFAULT_SESSION_FACTORY is not None:
        cached_key, cached_factory = _DEFAULT_SESSION_FACTORY
        if cached_key == db_file:
            return cached_factory

    sync_url = f"sqlite:///{db_file}"
    engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False}
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _DEFAULT_SESSION_FACTORY = (db_file, factory)
    return factory


def _reset_default_session_factory_cache() -> None:
    """Test hook — clear the cached sessionmaker."""
    global _DEFAULT_SESSION_FACTORY
    _DEFAULT_SESSION_FACTORY = None


class AlphaReportAnalystLoop(CadenceLoop):
    """Daily 18:00 IDT alpha-report analyst cadence loop.

    Constructor injection points so tests can drive the loop without
    touching the DB / SDK / live LLM:

    * ``schedule``          — overrides the cron/tz.
    * ``user_id``           — single-tenant for now (defaults ``"ariel"``).
    * ``session_factory``   — sync ``sessionmaker``; default builds from
                               ``get_settings().db_file``.
    * ``runner_fn``         — overrides
                               :func:`alpha_report_analyst_runner.
                               run_pending_batch`.
    * ``agent_factory``     — passed through to the runner so tests can
                               inject a mock :class:`AlphaReportAnalystAgent`.
    * ``batch_limit``       — overrides :data:`DEFAULT_BATCH_LIMIT`.
    * ``now_fn``            — overrides :func:`_utcnow` for determinism.
    """

    name = "alpha_report_analyst_daily"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        runner_fn: Callable[..., Any] | None = None,
        agent_factory: Callable[[], Any] | None = None,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule
            or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        if batch_limit <= 0:
            raise ValueError(
                f"batch_limit must be > 0; got {batch_limit!r}"
            )
        self.user_id = user_id
        self._session_factory = session_factory
        self._runner_fn = runner_fn
        self._agent_factory = agent_factory
        self._batch_limit = batch_limit
        self._now_fn = now_fn or _utcnow
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(
        self,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> dict | None:
        """Run :func:`run_pending_batch` once.

        Returns a summary dict with the runner's :class:`RunBatchResult`
        flattened into ``fetched`` / ``analyzed`` / ``skipped`` /
        ``predictions_written`` / ``monitor_flags_written``. Surfaced
        by the ``RegisteredScheduler`` adapter into ``job_runs.
        output_summary`` for admin-UI visibility.
        """
        self.last_output_summary = None

        run_at = (now or self._now_fn)()
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)

        _log.info(
            "alpha_report_analyst.tick.start",
            user_id=self.user_id,
            run_at=run_at.isoformat(),
            batch_limit=self._batch_limit,
        )

        # Run the sync body in a thread so the scheduler loop isn't
        # blocked by DB / LLM I/O and so ``BaseAgent.run``'s own
        # ``asyncio.run`` (inside the runner) doesn't collide with the
        # scheduler's running event loop.
        summary = await asyncio.to_thread(
            self._run_pipeline_sync,
            run_at=run_at,
        )
        self.last_output_summary = summary
        _log.info(
            "alpha_report_analyst.tick.done",
            user_id=self.user_id,
            **{k: v for k, v in summary.items() if k != "errors"},
        )
        return summary

    def _run_pipeline_sync(self, *, run_at: datetime) -> dict[str, Any]:
        factory = self._session_factory or _build_default_session_factory()
        session = factory()
        try:
            runner_fn = self._runner_fn or self._default_runner_fn()
            agent = (
                self._agent_factory()
                if self._agent_factory is not None
                else None
            )
            result = runner_fn(
                session,
                agent=agent,
                user_id=self.user_id,
                limit=self._batch_limit,
                now=run_at,
            )
            session.commit()
            return {
                "fetched": int(getattr(result, "fetched", 0)),
                "analyzed": int(getattr(result, "analyzed", 0)),
                "skipped": int(getattr(result, "skipped", 0)),
                "predictions_written": int(
                    getattr(result, "predictions_written", 0)
                ),
                "monitor_flags_written": int(
                    getattr(result, "monitor_flags_written", 0)
                ),
                "run_at": run_at.isoformat(),
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _default_runner_fn() -> Callable[..., Any]:
        from argosy.services.alpha_report_analyst_runner import (
            run_pending_batch,
        )
        return run_pending_batch


__all__ = [
    "DEFAULT_BATCH_LIMIT",
    "AlphaReportAnalystLoop",
    "alpha_report_analyst_metadata",
]
