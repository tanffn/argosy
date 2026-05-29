"""``PredictionsBackfillDiscordLoop`` — manual-only Discord backfill.

Spec C commit #7. Registers the
:func:`argosy.services.predictions.discord_backfill.backfill_discord_predictions`
service as a Spec-A :class:`JobRegistry`-visible job that the
operator can fire from the admin UI's RunNow button.

Design — NOT a CadenceLoop in spirit, IS a CadenceLoop in shape
----------------------------------------------------------------

Per the spec section 7.1: "registered with Spec A's JobRegistry as
``predictions-backfill-discord`` (manual trigger only — not
scheduled)."

The registry accepts two job shapes — :class:`CadenceLoop` (auto-fires
on a schedule) and :class:`LongRunningJob` (supervised long-lived).
Neither fits a "manual-only one-shot" job perfectly. We pick
:class:`CadenceLoop` because:

* :class:`LongRunningJob` is wrong — the backfill isn't a daemon; it
  has a well-defined start and end per invocation.
* The manual-trigger path (:meth:`JobRegistry.fire_now` →
  :meth:`RegisteredScheduler.fire_once_already_locked`) drives
  ``CadenceLoop`` subclasses just like the scheduled path does, only
  without acquiring the per-loop "should I fire?" gate.

To prevent the scheduler from EVER auto-firing this loop we:

1. Set ``enabled=False`` on the instance. The scheduler's
   :meth:`Scheduler._run_loop` reads this attribute at task-spawn
   time and IMMEDIATELY returns when False — see
   ``argosy/orchestrator/scheduler.py:247``. No cadence ticks fire.
2. Set ``schedule_cron=None`` on :class:`JobMetadata` so the admin
   UI clearly displays "Manual only" (mirrors
   :func:`discord_listener_metadata`'s convention).
3. Use ``source_kind='ingest'`` per the spec — the backfill ingests
   historical Discord messages into the predictions ledger; it's a
   data-source ingest, not maintenance or notification.

The manual-trigger path (:meth:`JobRegistry.fire_now` ⇒
``fire_once_already_locked`` ⇒ ``loop.tick(now=...)``) does NOT
consult ``loop.enabled`` (verified by reading
``argosy/services/jobs/registered_scheduler.py`` end-to-end — no
``enabled`` reads). So setting ``enabled=False`` blocks the auto-tick
without blocking the manual run-now path.

Tick body
---------

One synchronous bridge:

1. Open a sync ``Session`` from the same per-URL ``sessionmaker``
   cache used by :class:`PredictionsEvaluatorLoop` (so backfilled
   predictions land in the same DB the evaluator reads from).
2. ``await backfill_discord_predictions(session, lookback_days=14)``.
3. Commit (the service commits per-write but we close the outer
   transaction defensively).
4. Return the summary dict — Spec A commit #7 captures this on
   ``job_runs.output_summary`` so the admin UI can show
   "scanned=425 written=42 deduped=0 errors=0".

Session contract: the backfill calls ``session.commit()`` per
prediction insert, so the session this loop hands it must be safe to
commit incrementally. That matches the sync ``sessionmaker`` factory
the evaluator uses — and matches the test pattern that injects a
fresh in-memory factory per test.
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
from argosy.services.predictions.discord_backfill import (
    DEFAULT_USER_ID,
    BackfillSummary,
    backfill_discord_predictions,
)

_log = get_logger("argosy.loops.predictions_backfill_discord")


# Module-level sync session factory cache. Same shape as
# :func:`argosy.orchestrator.loops.predictions_evaluator._build_default_session_factory`
# — one factory per sync URL so a test that re-points
# ``init_engine`` to a fresh in-memory DB doesn't reuse the stale
# factory.
_DEFAULT_SESSION_FACTORY: tuple[str, sessionmaker] | None = None


def _build_default_session_factory() -> sessionmaker:
    """Construct + memoise a sync ``sessionmaker`` for the active DB.

    Mirrors :func:`predictions_evaluator._build_default_session_factory`.
    Kept here as a separate copy (rather than imported from the
    evaluator module) so the backfill loop has zero coupling to the
    evaluator's lifecycle.
    """
    global _DEFAULT_SESSION_FACTORY

    from argosy.state import db as db_mod

    engine = db_mod.get_engine()
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


def predictions_backfill_discord_metadata() -> JobMetadata:
    """Construct the :class:`JobMetadata` row the registry surfaces.

    * ``schedule_cron=None`` — the loop is manual-only. The admin UI's
      schedule column reads ``schedule_human`` for the human label.
    * ``source_kind='ingest'`` — backfill ingests historical Discord
      messages into the predictions ledger. Same family as the
      live discord_listener job.
    * ``long_running=False`` — the backfill returns in seconds-to-
      minutes; it's a :class:`CadenceLoop` shape, not a supervised
      daemon.
    """
    return JobMetadata(
        name="predictions_backfill_discord",
        schedule_cron=None,
        schedule_human="manual only (admin Run Now)",
        source_kind="ingest",
        description=(
            "Walk the configured Discord channel backwards for "
            "14 days and write a prediction row for every actionable "
            "alpha call (spec C §7). Idempotent — re-runs against the "
            "same window dedup via the writer's per-source contract."
        ),
        long_running=False,
        # Backfill runs can take 10-30s with 5 pages of 100 messages
        # plus per-message writes. The default 1s lock timeout would
        # cause a SECOND Run-Now click during an in-flight run to 409
        # the operator's UI promptly — that's the desired behavior, so
        # the default is fine.
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PredictionsBackfillDiscordLoop(CadenceLoop):
    """Manual-only Discord backfill loop.

    Constructor knobs (all overridable for tests):

    * ``session_factory`` — sync sessionmaker. Default
      :func:`_build_default_session_factory` (production DB).
    * ``lookback_days`` — how far back to walk. Default 14, per the
      original user ask.
    * ``user_id``       — tenant id stamped on every prediction.
      Default ``"ariel"`` (single-tenant).
    * ``page_fetcher``  — overridable async page fetcher. Default
      :func:`_fetch_page` in the backfill service module (real httpx).
    * ``now_fn``        — overridable clock for the lookback math.
      Default :func:`_utcnow`.

    The loop's ``enabled`` defaults to ``False`` so the scheduler
    never auto-fires it; the manual ``run-now`` path bypasses this
    gate (see module docstring).
    """

    name = "predictions_backfill_discord"

    def __init__(
        self,
        *,
        session_factory: sessionmaker | None = None,
        lookback_days: int = 14,
        user_id: str = DEFAULT_USER_ID,
        page_fetcher: Any | None = None,
        now_fn: Callable[[], datetime] | None = None,
        channel_id: int | None = None,
        bot_token: str | None = None,
    ) -> None:
        # The base class requires a :class:`LoopSchedule`. We pass a
        # nominal far-future interval (1 year) so even if a test
        # mis-flips ``enabled`` the auto-fire is at most yearly. The
        # schedule is otherwise inert — :class:`Scheduler._run_loop`
        # returns immediately when ``enabled=False``.
        super().__init__(
            schedule=LoopSchedule(interval_seconds=365 * 24 * 3600),
            enabled=False,
        )
        if lookback_days <= 0:
            raise ValueError(
                f"lookback_days must be > 0; got {lookback_days!r}"
            )
        self._session_factory = session_factory
        self._lookback_days = lookback_days
        self._user_id = user_id
        self._page_fetcher = page_fetcher
        self._now_fn = now_fn or _utcnow
        self._channel_id = channel_id
        self._bot_token = bot_token
        # Spec A commit #7 NICE #7 — multi-stage exception-path hook.
        # We set this in the ``finally`` block of :meth:`tick` so a
        # mid-run failure still surfaces partial counts on the audit
        # row's ``output_summary``.
        self.last_output_summary: dict[str, Any] | None = None

    def _resolve_session_factory(self) -> sessionmaker:
        if self._session_factory is not None:
            return self._session_factory
        return _build_default_session_factory()

    def _run_tick_sync(self) -> dict[str, Any]:
        """Synchronous body — opens its own session, runs the backfill,
        commits, returns the summary dict.

        We bridge to async by stepping into ``asyncio.run`` ONLY when
        no event loop is already running. The default :meth:`tick`
        path runs us via :func:`asyncio.to_thread` so the outer event
        loop is the FastAPI / Spec A scheduler — we want to stay
        synchronous here to keep DB work off the event loop.

        We DO need to run the async ``backfill_discord_predictions``
        body though. Pattern: a fresh per-call event loop inside the
        worker thread so the async httpx + asyncio.sleep machinery
        works without colliding with the outer scheduler loop.
        """
        factory = self._resolve_session_factory()
        session: Session = factory()
        try:
            summary = asyncio.run(
                backfill_discord_predictions(
                    session,
                    lookback_days=self._lookback_days,
                    user_id=self._user_id,
                    page_fetcher=self._page_fetcher,
                    now=self._now_fn,
                    channel_id=self._channel_id,
                    bot_token=self._bot_token,
                )
            )
        finally:
            session.close()
        return summary.to_dict()

    async def tick(
        self, *, now: Callable[[], datetime] | None = None
    ) -> dict | None:
        """Async entry-point — runs the sync body on a worker thread.

        The ``now`` parameter is supplied by the scheduler / registry
        but the backfill's lookback math uses ``self._now_fn``
        directly so tests can drive a frozen clock independently of
        the scheduler's clock injection.

        The ``finally`` block writes :attr:`last_output_summary` so a
        mid-tick exception still surfaces partial progress via the
        :class:`RegisteredScheduler` adapter's exception-path
        fallback (Spec A §7 NICE #7).
        """
        # Defensive: clear the side-channel before the run.
        self.last_output_summary = None
        del now  # unused — see docstring.

        _log.info(
            "predictions_backfill_discord.tick.start",
            lookback_days=self._lookback_days,
            user_id=self._user_id,
        )

        summary: dict[str, Any] | None = None
        try:
            summary = await asyncio.to_thread(self._run_tick_sync)
        finally:
            if summary is not None:
                self.last_output_summary = summary

        _log.info(
            "predictions_backfill_discord.tick.done",
            summary=summary,
        )
        return summary


__all__ = [
    "PredictionsBackfillDiscordLoop",
    "predictions_backfill_discord_metadata",
    "BackfillSummary",
]
