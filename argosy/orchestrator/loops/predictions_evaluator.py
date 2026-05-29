"""``PredictionsEvaluatorLoop`` — daily 03:30 IDT outcome-evaluator cron.

Spec C commit #4. Wires the evaluator service + retention pass into one
:class:`CadenceLoop` registered with Sprint A's :class:`JobRegistry`.

Schedule: ``30 3 * * *`` Asia/Jerusalem (03:30 daily), aligned with
:class:`JobRunsRetentionLoop` which fires at the same time — they don't
compete for the same DB rows so the schedule overlap is intentional
(and minimises the number of distinct cron rows the operator has to
reason about).

Two-stage tick:

1. :func:`argosy.services.predictions.evaluator.run_evaluator_batch` —
   pick up to ``batch_size`` predictions due for scoring + write
   ``prediction_outcomes`` rows.
2. :func:`argosy.services.predictions.retention.run_retention_pass` —
   flip ``archived=1`` on evaluated predictions older than the
   retention window.

Both stages share one SQLAlchemy session + one transaction; a crash
mid-stage rolls back the entire tick so the next-day run sees the
exact same pre-tick state. The idempotency contracts on both stages
(``(prediction_id, evaluation_method)`` UNIQUE for the evaluator;
``archived = 0 AND has_outcome`` predicate for retention) make
re-running safe.

Sync session contract — same as state_observer / news_daily. The
loop builds its own sync ``Session`` per tick from a module-level
:class:`sessionmaker` cache so the evaluator + retention pass can
run synchronously without bouncing through ``asyncio.run`` on every
DB call.

Side-channel: ``self.last_output_summary`` is set in a ``finally``
block so a mid-tick exception still surfaces partial progress via the
:class:`RegisteredScheduler` adapter's exception-path fallback (spec A
commit #7 NICE #7).
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
from argosy.services.predictions.evaluator import (
    EvaluatorSummary,
    PriceFetcher,
    default_price_fetcher,
    run_evaluator_batch,
)
from argosy.services.predictions.retention import (
    DEFAULT_ARCHIVE_DAYS,
    DEFAULT_RETENTION_DAYS,
    RetentionSummary,
    run_retention_pass,
)

_log = get_logger("argosy.loops.predictions_evaluator")


# Default cron + tz — kept in sync with ``cadences.predictions_evaluator``
# in ``argosy/agent_settings.py``. 03:30 Asia/Jerusalem = same slot as
# ``job_runs_retention``; they don't touch the same rows.
_DEFAULT_CRON = "30 3 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"

# Production default batch size — bounded so a single tick never holds
# the DB write-lock for more than a few seconds even on a backlog day.
_DEFAULT_BATCH_SIZE: int = 200


def predictions_evaluator_metadata() -> JobMetadata:
    """Construct the :class:`JobMetadata` row the registry surfaces.

    ``source_kind='maintenance'`` — the evaluator is bookkeeping work
    on past predictions, not an external ingest or a user-facing
    monitor flag. Same family as :func:`job_runs_retention_metadata`.
    """
    return JobMetadata(
        name="predictions_evaluator",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 03:30 Asia/Jerusalem",
        source_kind="maintenance",
        description=(
            "Daily outcome evaluator — scores predictions whose "
            "evaluation_due_at has passed (spec §3.1), then runs a "
            "retention pass that archives evaluated predictions older "
            "than the retention window (spec §9.1)."
        ),
        long_running=False,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Module-level sync session factory cache. Same pattern as
# ``news_daily.py`` / ``state_observer.py`` — the evaluator + retention
# pass both expect a sync Session.
_DEFAULT_SESSION_FACTORY: tuple[str, sessionmaker] | None = None


def _build_default_session_factory() -> sessionmaker:
    """Construct + memoise a sync ``sessionmaker`` for the active DB.

    Reads ``argosy.state.db._engine`` to obtain the URL that's already
    initialised (the FastAPI startup hook calls ``init_engine`` before
    any loop fires). Cached per-URL so a test that re-points
    ``init_engine`` to a fresh in-memory DB doesn't reuse the stale
    factory.
    """
    global _DEFAULT_SESSION_FACTORY

    from argosy.state import db as db_mod

    engine = db_mod.get_engine()
    # The async engine's URL — sub the +aiosqlite driver out for the
    # sync engine we want here.
    async_url = str(engine.url)
    sync_url = async_url.replace("+aiosqlite", "")

    if _DEFAULT_SESSION_FACTORY is not None:
        cached_url, cached_factory = _DEFAULT_SESSION_FACTORY
        if cached_url == sync_url:
            return cached_factory

    import sqlalchemy as sa

    sync_engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False}
    )
    factory = sessionmaker(bind=sync_engine, expire_on_commit=False)
    _DEFAULT_SESSION_FACTORY = (sync_url, factory)
    return factory


class PredictionsEvaluatorLoop(CadenceLoop):
    """Daily evaluator + retention loop.

    Constructor accepts injection points so tests can drive the loop
    deterministically:

    * ``schedule``        — overrides the default 03:30 IDT cron.
    * ``session_factory`` — overrides the default sync sessionmaker
      lookup; tests pass a fixture-built factory pointed at a per-test
      SQLite file.
    * ``price_fetcher``   — overrides
      :func:`default_price_fetcher`; tests inject a deterministic
      stub that returns a pre-built list of :class:`Bar` instances.
    * ``batch_size``      — overrides the per-tick LIMIT on the
      due-selection query.
    * ``retention_days`` / ``archive_days`` — override the spec §9.1
      defaults; tests pass small numbers (e.g. 1 day) to verify the
      retention path without seeding 365-day-old fixtures.
    * ``now_fn``          — overrides :func:`_utcnow`; tests pass a
      frozen-clock callable so the due-query resolves deterministically.
    """

    name = "predictions_evaluator"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        session_factory: sessionmaker | None = None,
        price_fetcher: PriceFetcher | None = None,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        archive_days: int = DEFAULT_ARCHIVE_DAYS,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule
            or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        if batch_size <= 0:
            raise ValueError(
                f"batch_size must be > 0; got {batch_size!r}"
            )
        if retention_days < 0:
            raise ValueError(
                f"retention_days must be >= 0; got {retention_days!r}"
            )
        if archive_days < 0:
            raise ValueError(
                f"archive_days must be >= 0; got {archive_days!r}"
            )
        self._session_factory = session_factory
        self._price_fetcher = price_fetcher or default_price_fetcher
        self._batch_size = batch_size
        self._retention_days = retention_days
        self._archive_days = archive_days
        self._now_fn = now_fn or _utcnow
        self.last_output_summary: dict[str, Any] | None = None

    def _resolve_session_factory(self) -> sessionmaker:
        if self._session_factory is not None:
            return self._session_factory
        return _build_default_session_factory()

    def _run_tick_sync(self, now_dt: datetime) -> dict[str, Any]:
        """Synchronous body — opens its own session, runs both passes,
        commits, returns the combined summary dict.

        Factored out so :meth:`tick` can wrap it in
        :func:`asyncio.to_thread` without the orchestration logic
        leaking into the async coroutine. Same pattern as
        ``news_daily``.
        """
        factory = self._resolve_session_factory()
        session: Session = factory()
        try:
            ev_summary: EvaluatorSummary = run_evaluator_batch(
                session,
                now=now_dt,
                batch_size=self._batch_size,
                price_fetcher=self._price_fetcher,
            )
            ret_summary: RetentionSummary = run_retention_pass(
                session,
                now=now_dt,
                retention_days=self._retention_days,
                archive_days=self._archive_days,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        return {
            "evaluator": ev_summary.to_dict(),
            "retention": ret_summary.to_dict(),
        }

    async def tick(
        self, *, now: Callable[[], datetime] | None = None
    ) -> dict | None:
        """Async entry-point — runs the sync body on a worker thread.

        ``now`` (when passed) overrides the loop's own ``now_fn`` so
        the scheduler's "what time is it really?" tracking can flow
        through; tests that exercise the deterministic-time contract
        pass it explicitly.

        The ``finally`` block writes ``self.last_output_summary`` so a
        mid-tick exception still surfaces partial progress via the
        :class:`RegisteredScheduler` adapter (Spec A §7 NICE #7).
        """
        self.last_output_summary = None

        now_dt = (now or self._now_fn)()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        _log.info(
            "predictions_evaluator.tick.start",
            now=now_dt.isoformat(),
            batch_size=self._batch_size,
            retention_days=self._retention_days,
            archive_days=self._archive_days,
        )

        summary: dict[str, Any] | None = None
        try:
            summary = await asyncio.to_thread(
                self._run_tick_sync, now_dt
            )
        finally:
            if summary is not None:
                self.last_output_summary = summary

        _log.info(
            "predictions_evaluator.tick.done",
            summary=summary,
        )
        return summary


__all__ = [
    "PredictionsEvaluatorLoop",
    "predictions_evaluator_metadata",
]
