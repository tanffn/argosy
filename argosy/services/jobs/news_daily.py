"""``NewsDailyJob`` — 17:00 IDT daily news pipeline (Spec A commit #7).

Wraps the two-stage news pipeline as a single :class:`CadenceLoop`:

  Stage 1 — :func:`argosy.services.news_ingest.run_news_ingest`
            (deterministic extractor; no LLM).
  Stage 2 — :func:`argosy.services.news_analyst_runner.run_news_signal_analysis`
            (Opus analyst over batches of ≤20 signals).

Both stages share **one** sync SQLAlchemy ``Session`` so a partial
ingest doesn't leak rows the analyst can't see. ``tick()`` runs the
sync work via :func:`asyncio.to_thread` because the analyst runner uses
:func:`asyncio.run` internally and cannot nest inside a running loop.

Per-stage outcome capture (codex NICE #7): when Stage 2 raises, Stage 1's
counts are still surfaced through ``self.last_output_summary`` — set in
a ``finally`` block so the
:class:`~argosy.services.jobs.registered_scheduler.RegisteredScheduler`
adapter can record "ingest ok, analyze error" in ``job_runs.output_summary``
even though the tick itself re-raised.

Same-code-path contract: the scheduler fires this on the 17:00 cadence
AND the manual ``Run now`` path (commit #4's
``POST /api/jobs/news_daily/run-now``) goes through the same
:meth:`tick` body — no parallel "manual" variant.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from argosy.agents.news_signal_analyst import NewsSignalAnalystAgent
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata
from argosy.services.news_analyst_runner import (
    AnalysisRunResult,
    run_news_signal_analysis,
)
from argosy.services.news_ingest import NewsIngestResult, run_news_ingest

_log = get_logger("argosy.jobs.news_daily")


# Default cron + tz — kept in sync with cadences.news_daily in
# agent_settings.py. The cadence-config value takes precedence at boot
# time (commit #3b's startup hook will pass `cadences.news_daily` through
# `LoopSchedule.from_config`); this default lets tests construct the job
# without a full AgentSettings round-trip.
_DEFAULT_CRON = "0 17 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"


def news_daily_metadata() -> JobMetadata:
    """Construct the :class:`JobMetadata` row for the registry.

    Imported by ``argosy/api/main.py``'s guarded-import block (already
    present from commit #3b); the registration call happens there.
    """
    return JobMetadata(
        name="news_daily",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 17:00 IDT",
        source_kind="ingest",
        description=(
            "Daily news pipeline — Stage 1 RSS+macro_feed ingest + Stage 2 "
            "Opus analyst classification. Runs at 17:00 IL-local; manual "
            "Run now goes through the same tick body."
        ),
        long_running=False,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Module-level cache for the sync engine + sessionmaker. We build it
# lazily on first use (so import-time has no side effects) and reuse
# across ticks (so we don't churn an engine + connection pool every
# 17:00 IDT). Keyed by db_file path so a settings-reload that points
# at a different DB transparently rebuilds.
_DEFAULT_SESSION_FACTORY: tuple[str, sessionmaker] | None = None


def _build_default_session_factory() -> sessionmaker:
    """Return the cached sync ``sessionmaker`` bound to the configured DB.

    Mirrors the pattern in ``argosy/cli/expenses_admin.py`` — the news
    pipeline services (``run_news_ingest`` + ``run_news_signal_analysis``)
    require a SYNC ``Session``; the async ``argosy.state.db.get_session``
    yields an :class:`AsyncSession` which they cannot consume.

    Lifecycle: the engine + sessionmaker are built on first use and
    reused for the process lifetime. Rebuilds only when ``db_file``
    changes (e.g. a test reloads settings to point at a fresh
    ``tmp_path``). Tests inject their own factory via the constructor.

    Codex review (commit #7) flagged a per-tick rebuild here that
    leaked engines + connection pools at 17:00 IDT every day; this
    cache fixes that.
    """
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
    """Test hook — clear the cached sessionmaker so a subsequent call
    rebuilds from the current settings. Production code never invokes
    this; pytest fixtures using ``monkeypatch.setenv("ARGOSY_HOME", ...)``
    may need it if they rely on the default factory."""
    global _DEFAULT_SESSION_FACTORY
    _DEFAULT_SESSION_FACTORY = None


class NewsDailyJob(CadenceLoop):
    """Daily news ingest + analyst loop.

    Constructor accepts optional injection points so the unit tests can
    swap in stubs without touching the DB or the SDK:

    * ``schedule``         — overrides the default cron/tz (tests pass
                              an interval-based ``LoopSchedule``).
    * ``session_factory``  — sync ``sessionmaker``; default builds one
                              from ``get_settings().db_file``.
    * ``ingest_fn``        — overrides ``run_news_ingest``.
    * ``analyst_fn``       — overrides ``run_news_signal_analysis``.
    * ``agent_factory``    — overrides ``NewsSignalAnalystAgent``.
    * ``user_holdings``    — ticker symbols threaded into the analyst's
                              materiality context. Default ``[]`` for now;
                              production will pull from the user's
                              positions before the analyst call.
    """

    name = "news_daily"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        ingest_fn: Callable[..., NewsIngestResult] | None = None,
        analyst_fn: Callable[..., AnalysisRunResult] | None = None,
        agent_factory: Callable[[], NewsSignalAnalystAgent] | None = None,
        user_holdings: list[str] | None = None,
        tickers: list[str] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule
            or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._session_factory = session_factory
        self._ingest_fn = ingest_fn or run_news_ingest
        self._analyst_fn = analyst_fn or run_news_signal_analysis
        self._agent_factory = agent_factory or (
            lambda: NewsSignalAnalystAgent(user_id=self.user_id)
        )
        self._user_holdings = user_holdings or []
        self._tickers = tickers
        #: Populated in :meth:`tick`'s ``finally`` so the
        #: :class:`RegisteredScheduler` adapter can read partial-progress
        #: results when Stage 2 raises (codex NICE #7).
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(
        self, *, now: Callable[[], datetime] | None = None
    ) -> dict | None:
        """Run Stage 1 ingest + Stage 2 analyst in one sync session.

        Returns the ``output_summary`` dict on success; raises (after
        populating ``self.last_output_summary`` in ``finally``) when
        Stage 2 fails. Stage 1's counts are always recorded — partial
        progress is observable in both the success and the
        Stage-2-failure cases.
        """
        run_at = (now or _utcnow)()
        _log.info("news_daily.tick.start", run_at=run_at.isoformat())

        # Reset the side-channel BEFORE any work — codex review (commit
        # #7) flagged a stale-summary bug where session_factory()
        # raising before the `try/finally` block would leave the
        # adapter reading the PRIOR tick's summary. By clearing here,
        # any unhandled-before-finally raise leaves last_output_summary
        # at None and the adapter records NULL for output_summary.
        self.last_output_summary = None

        factory = self._session_factory or _build_default_session_factory()

        # The sync session work runs in a thread so the analyst runner's
        # internal ``asyncio.run`` doesn't collide with our event loop.
        # Tests that pass a sync ``session_factory`` directly still go
        # through this path — ``asyncio.to_thread`` is a no-op for already
        # synchronous work and keeps the production + test paths
        # identical.
        return await asyncio.to_thread(self._run_stages_sync, factory)

    def _run_stages_sync(
        self, session_factory: Callable[[], Session]
    ) -> dict[str, Any]:
        """Synchronous body — one Session crosses both stages.

        Splitting this out lets tests assert on session sharing (the
        same ``Session`` object is passed to ``ingest_fn`` and
        ``analyst_fn``) without dealing with the async wrapper.
        """
        stage1_result: NewsIngestResult | None = None
        stage2_result: AnalysisRunResult | None = None
        stage1_status = "pending"
        stage2_status = "pending"
        stage1_error: str | None = None
        stage2_error: str | None = None

        session = session_factory()
        try:
            # ------------------------------------------------------------
            # Stage 1 — deterministic ingest. No LLM.
            # ------------------------------------------------------------
            try:
                stage1_result = self._ingest_fn(
                    session, tickers=self._tickers,
                )
                stage1_status = "ok"
                # Commit before Stage 2 so the analyst sees Stage 1's
                # rows even if Stage 2 rolls back.
                session.commit()
            except Exception as exc:
                stage1_status = "error"
                stage1_error = str(exc)
                _log.exception("news_daily.stage1_failed")
                session.rollback()
                # Re-raise: Stage 1 failure means we have nothing for
                # Stage 2 to analyze anyway.
                raise

            # ------------------------------------------------------------
            # Stage 2 — Opus analyst over unanalyzed rows.
            # Agent construction lives INSIDE the try block (codex commit
            # #7 review): NewsSignalAnalystAgent.__init__ does SDK setup
            # that can fail (missing API key, network probe, etc.). If
            # agent construction raises, that's a Stage 2 failure mode —
            # classifying it as `analyze='pending'` would lie to the
            # operator.
            # ------------------------------------------------------------
            try:
                agent = self._agent_factory()
                stage2_result = self._analyst_fn(
                    session,
                    agent=agent,
                    user_holdings=self._user_holdings,
                )
                stage2_status = "ok"
                session.commit()
            except Exception as exc:
                stage2_status = "error"
                stage2_error = str(exc)
                _log.exception("news_daily.stage2_failed")
                session.rollback()
                raise
        finally:
            # Populate the side-channel BEFORE closing the session so the
            # adapter reads a complete dict even on the exception path.
            self.last_output_summary = _build_summary(
                stage1_result=stage1_result,
                stage2_result=stage2_result,
                stage1_status=stage1_status,
                stage2_status=stage2_status,
                stage1_error=stage1_error,
                stage2_error=stage2_error,
            )
            session.close()

        _log.info(
            "news_daily.tick.done",
            ingested=stage1_result.persisted if stage1_result else 0,
            analyzed=stage2_result.analyzed if stage2_result else 0,
        )
        return self.last_output_summary


def _build_summary(
    *,
    stage1_result: NewsIngestResult | None,
    stage2_result: AnalysisRunResult | None,
    stage1_status: str,
    stage2_status: str,
    stage1_error: str | None,
    stage2_error: str | None,
) -> dict[str, Any]:
    """Render the ``output_summary`` dict in the spec's shape.

    Always returns a dict — even when both stages failed, the operator
    sees ``stages={ingest: <status>, analyze: <status>}`` and an empty
    counts block. This keeps the ``job_runs.output_summary`` column
    queryable: every news_daily row has the same top-level keys.
    """
    counts: dict[str, int] = {
        "ingested_fetched": stage1_result.fetched if stage1_result else 0,
        "ingested_persisted": stage1_result.persisted if stage1_result else 0,
        "ingested_duplicates": stage1_result.duplicates if stage1_result else 0,
        "analyzed": stage2_result.analyzed if stage2_result else 0,
        "analyzed_batches": stage2_result.batches if stage2_result else 0,
    }
    stage_errors: dict[str, str] = {}
    if stage1_error:
        stage_errors["ingest"] = stage1_error
    if stage2_error:
        stage_errors["analyze"] = stage2_error

    notes = (
        f"by_source={stage1_result.by_source!r}"
        if stage1_result is not None
        else "no_stage1_result"
    )

    return {
        "counts": counts,
        "stages": {
            "ingest": stage1_status,
            "analyze": stage2_status,
        },
        "stage_errors": stage_errors,
        "notes": notes,
    }


__all__ = [
    "NewsDailyJob",
    "news_daily_metadata",
]
