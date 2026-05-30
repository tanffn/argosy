"""``InferredLifeEventDetectorLoop`` — daily 03:00 IDT detector cron.

Spec E commit #5.  Wires the
``argosy/services/inferred_life_event_detector.py`` orchestrator into
a :class:`CadenceLoop` registered with Sprint A's :class:`JobRegistry`.

Schedule: ``0 3 * * *`` Asia/Jerusalem (03:00 daily) — after midnight
(idle window), before the 17:00 IDT news pipeline + state observer
runs.  Per Ariel's locked decision in spec §5.5.

Single-stage tick: calls
:func:`argosy.services.inferred_life_event_detector.run_detector` for
``user_id='ariel'`` and returns the
:class:`~argosy.services.inferred_life_event_detector.DetectorSummary`
as a dict for the JobRegistry's last-output cache.

The loop holds NO state across ticks — idempotency is enforced by
the database (the UNIQUE(user_id, pattern, evidence_window_start,
evidence_window_end) constraint on
``inferred_life_event_findings``).  Re-running the loop on the same
window is a no-op (every INSERT raises ``IntegrityError`` which is
caught + treated as "already processed").

Sync session contract — same as
``state_observer.py`` / ``predictions_evaluator.py``: the detector
runs against a sync ``Session`` so the heuristics + DB writes don't
need to bounce through ``asyncio.run`` on every call.  The async
``tick`` wraps the sync body in :func:`asyncio.to_thread` so the
scheduler's event loop isn't blocked.

Side-channel: ``self.last_output_summary`` is set in a ``finally``
block so a mid-tick exception still surfaces partial progress via
the :class:`RegisteredScheduler` adapter (Spec A §7 NICE #7).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.inferred_life_event_detector import (
    DetectorSummary,
    run_detector,
)
from argosy.services.jobs.registry import JobMetadata

_log = get_logger("argosy.loops.inferred_life_event_detector")


# Default cron + tz — kept in sync with
# ``cadences.inferred_life_event_detector`` in
# ``argosy/agent_settings.py``.  03:00 Asia/Jerusalem = idle window
# before the 17:00 IDT news + state observer pipeline.
_DEFAULT_CRON = "0 3 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"

# Default lookback — 365 days (the spec's 12-month window).  Tunable
# per the backfill verification commit.
_DEFAULT_LOOKBACK_DAYS: int = 365


def inferred_life_event_detector_metadata() -> JobMetadata:
    """Construct the :class:`JobMetadata` row the registry surfaces.

    ``source_kind='monitor'`` — the detector's output materialises as
    action proposals on the Red-Flag Strip family, same as the
    state_observer.  Matches the SDD §11.1 source_kind taxonomy.
    """
    return JobMetadata(
        name="inferred_life_event_detector",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 03:00 Asia/Jerusalem",
        source_kind="monitor",
        description=(
            "Inferred life-event detector — reads expense_transactions, "
            "applies five heuristics + conflict resolver + continuity "
            "check, persists findings to inferred_life_event_findings, "
            "fires action_proposer for non-dismissed findings. Shadow "
            "mode for new users (< 30d). 03:00 IDT daily."
        ),
        long_running=False,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Module-level sync session factory cache.  Same pattern as
# ``state_observer.py`` / ``predictions_evaluator.py``.
_DEFAULT_SESSION_FACTORY: tuple[str, sessionmaker] | None = None


def _build_default_session_factory() -> sessionmaker:
    """Return the cached sync ``sessionmaker`` bound to the configured DB.

    Lazy + cached so import-time has no side effects and we don't
    churn an engine pool every 03:00 IDT.  Rebuilds when ``db_file``
    changes (test reloads of settings).
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
    rebuilds from the current settings."""
    global _DEFAULT_SESSION_FACTORY
    _DEFAULT_SESSION_FACTORY = None


class InferredLifeEventDetectorLoop(CadenceLoop):
    """Daily inferred-life-event detector loop.

    Constructor injection points so tests can drive the loop without
    touching the DB / proposer:

    * ``schedule``         — overrides the cron/tz.
    * ``user_id``          — single-tenant for now (defaults
      ``"ariel"``).
    * ``session_factory``  — sync ``sessionmaker``; default builds
      from ``get_settings().db_file``.
    * ``lookback_days``    — rolling-window size; defaults 365.
    * ``shadow_mode``      — None lets the detector resolve from the
      user's account age; True/False pins it.
    * ``proposer_runner``  — overrides the production
      action_proposer_runner; tests pass a stub coroutine factory.
    * ``now_fn``           — overrides :func:`_utcnow`; tests pin time.
    """

    name = "inferred_life_event_detector"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
        shadow_mode: bool | None = None,
        proposer_runner: Callable[..., Any] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule
            or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        if lookback_days <= 0:
            raise ValueError(
                "lookback_days must be > 0; "
                f"got {lookback_days!r}"
            )
        self.user_id = user_id
        self._session_factory = session_factory
        self._lookback_days = lookback_days
        self._shadow_mode = shadow_mode
        self._proposer_runner = proposer_runner
        self._now_fn = now_fn or _utcnow
        #: Surfaced by the ``RegisteredScheduler`` adapter even on the
        #: exception path so partial progress (some findings persisted
        #: but proposer crashed) is observable.
        self.last_output_summary: dict[str, Any] | None = None

    def _resolve_session_factory(self) -> sessionmaker:
        if self._session_factory is not None:
            return self._session_factory
        return _build_default_session_factory()

    def _run_tick_sync(self, now_dt: datetime) -> dict[str, Any]:
        """Sync body — opens a session, runs the detector, returns
        the summary dict.

        Factored so :meth:`tick` can wrap it in
        :func:`asyncio.to_thread` without the orchestration logic
        leaking into the async coroutine.  Mirrors the
        ``predictions_evaluator.py`` pattern.
        """
        factory = self._resolve_session_factory()
        session: Session = factory()
        try:
            summary: DetectorSummary = run_detector(
                session,
                self.user_id,
                lookback_days=self._lookback_days,
                shadow_mode=self._shadow_mode,
                now=now_dt,
                proposer_runner=self._proposer_runner,
            )
        finally:
            session.close()
        return summary.to_dict()

    async def tick(
        self, *, now: Callable[[], datetime] | None = None
    ) -> dict | None:
        """Async entry-point — runs the sync body on a worker thread.

        ``now`` (when passed) overrides the loop's own ``now_fn`` so
        the scheduler's "what time is it really?" tracking can flow
        through; tests that exercise deterministic-time contracts
        pass it explicitly.

        The ``finally`` block writes ``self.last_output_summary`` so
        a mid-tick exception still surfaces partial progress via the
        :class:`RegisteredScheduler` adapter (Spec A §7 NICE #7).
        """
        self.last_output_summary = None

        now_dt = (now or self._now_fn)()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        _log.info(
            "inferred_life_event_detector.tick.start",
            user_id=self.user_id,
            now=now_dt.isoformat(),
            lookback_days=self._lookback_days,
            shadow_mode=self._shadow_mode,
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
            "inferred_life_event_detector.tick.done",
            user_id=self.user_id,
            summary=summary,
        )
        return summary


__all__ = [
    "InferredLifeEventDetectorLoop",
    "inferred_life_event_detector_metadata",
]
