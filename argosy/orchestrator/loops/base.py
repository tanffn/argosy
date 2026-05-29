"""Base abstractions for cadence loops (SDD §5)."""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from argosy.agent_settings import CadenceConfig

try:
    # `croniter` is the only practical pure-python cron parser. We add it as
    # a direct dependency; if it's missing, fall back to interval-only.
    from croniter import croniter as _croniter  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only when dep missing
    _croniter = None  # type: ignore[assignment]


class TickStatus(str, enum.Enum):
    OK = "ok"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class LoopSchedule:
    """Resolved schedule for a loop.

    Either `cron` or `interval_seconds` must be set. `market_hours_only`
    is informational; the scheduler checks the market-open trigger
    separately.
    """

    cron: str | None = None
    interval_seconds: int | None = None
    market_hours_only: bool = False
    timezone: str = "Asia/Jerusalem"

    @classmethod
    def from_config(cls, cfg: CadenceConfig) -> "LoopSchedule":
        interval: int | None = cfg.interval_seconds
        if interval is None and cfg.interval_minutes is not None:
            interval = int(cfg.interval_minutes) * 60
        return cls(
            cron=cfg.cron,
            interval_seconds=interval,
            market_hours_only=cfg.market_hours_only,
            timezone=cfg.timezone,
        )

    def next_due_after(self, ref: datetime) -> datetime:
        """Compute the next-due timestamp after `ref`.

        For cron-driven loops, uses `croniter` and evaluates the cron
        expression in ``self.timezone`` (Spec A commit #2 — codex BLOCKER
        #3). Prior behavior ignored ``self.timezone`` and evaluated the
        cron in UTC, which meant ``cron="0 9 * * *"`` with
        ``timezone="Asia/Jerusalem"`` fired at 9:00 UTC instead of
        9:00 IDT — a 2-3 hour offset depending on DST. All eight existing
        cron-driven loops in ``CadencesBlock`` shipped with the implicit
        IL-local intent and are corrected by this fix.

        For interval-driven loops, adds ``interval_seconds``. If neither
        is set, returns ref+1h (a defensive fallback so the scheduler
        never busy-loops).

        Returned value is always a tz-aware UTC datetime.
        """
        if self.cron and _croniter is not None:
            try:
                from zoneinfo import ZoneInfo

                tz = ZoneInfo(self.timezone)
                # `ref` is conventionally UTC at the call sites; coerce
                # to ensure astimezone works whether or not it carries
                # an explicit tzinfo.
                if ref.tzinfo is None:
                    ref = ref.replace(tzinfo=timezone.utc)
                ref_local = ref.astimezone(tz)
                ci = _croniter(self.cron, ref_local)
                next_local = ci.get_next(datetime)
                # croniter returns a naive datetime; reattach the local
                # tz before converting back to UTC.
                if next_local.tzinfo is None:
                    next_local = next_local.replace(tzinfo=tz)
                return next_local.astimezone(timezone.utc)
            except Exception:  # pragma: no cover - malformed cron string
                return ref + timedelta(hours=1)
        if self.interval_seconds and self.interval_seconds > 0:
            return ref + timedelta(seconds=self.interval_seconds)
        return ref + timedelta(hours=1)


class CadenceLoop(abc.ABC):
    """Abstract cadence loop.

    Subclasses implement `tick(...)` (the actual work) and provide a
    `name`. The scheduler calls `tick()` at the loop's cadence and
    persists the result in `cadence_state`.
    """

    #: Stable name; used as `cadence_state.loop_name` PK.
    name: str = "base"

    def __init__(self, *, schedule: LoopSchedule, enabled: bool = True) -> None:
        self.schedule = schedule
        self.enabled = enabled

    @abc.abstractmethod
    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        """Run one tick of work. Raise to signal failure."""
        raise NotImplementedError


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# LongRunningJob (Spec A commit #5)
# ---------------------------------------------------------------------------


#: Allowed values for ``LongRunningJob.connection_status()``.
ConnectionStatus = Literal["connected", "reconnecting", "stopped"]

#: Allowed values for ``LongRunningJob.exit_intent``.
#:
#: * ``"unset"``       — initial value (before ``run()`` exits). The
#:                       supervisor disambiguates based on HOW ``run()``
#:                       exited:
#:                         - normal return + still ``"unset"`` → coerced
#:                           to ``"clean"`` (returning without raising IS
#:                           a clean exit by definition).
#:                         - raised exception + still ``"unset"`` → coerced
#:                           to ``"crashed"``.
#:                       Subclasses SHOULD set the intent explicitly to
#:                       avoid relying on the supervisor's coercion.
#: * ``"clean"``       — upstream closed cleanly; no auto-restart in v1
#:                       (Ariel reconnects via Run-now; spec §3, IMPORTANT #3).
#: * ``"operator_stop"`` — operator clicked Stop / called
#:                         :meth:`JobRegistry.cancel_long_running`; no auto-restart.
#: * ``"crashed"``     — unexpected exception or non-clean exit; supervisor
#:                       restarts with exponential backoff (1s → 2s → ...
#:                       capped at 60s).
ExitIntent = Literal["unset", "operator_stop", "clean", "crashed"]


class LongRunningJob(abc.ABC):
    """A job whose ``run()`` is itself a long-lived coroutine.

    Spec A §3 — sibling class to :class:`CadenceLoop`. DOES NOT extend
    :class:`CadenceLoop` because the shapes are different:

    * :class:`CadenceLoop` ticks at a cadence; each tick is a short
      coroutine the scheduler awaits.
    * :class:`LongRunningJob` runs ONCE per (connect, disconnect)
      cycle; the supervisor in :class:`JobRegistry` decides whether to
      restart it based on :attr:`exit_intent`.

    Contract:

    * :meth:`run` — long-lived coroutine; returns only when the job
      naturally completes (upstream closed, operator stopped, crash).
      Before returning, the job SHOULD set ``self._exit_intent`` to
      one of ``{"clean", "operator_stop", "crashed"}`` so the supervisor
      can decide restart semantics. If left ``"unset"`` the supervisor
      disambiguates based on HOW ``run()`` exited: normal return → coerced
      to ``"clean"``; raised exception → coerced to ``"crashed"``. See
      :attr:`exit_intent` for the full table.
    * :meth:`connection_status` — fast read returning
      ``"connected" | "reconnecting" | "stopped"``. The registry polls
      this for the UI's ``last_run_status`` field rather than waiting
      for ``run()`` to return.
    * :meth:`cancel` — async cancel hook. The supervisor calls this
      from :meth:`JobRegistry.cancel_long_running`. Default behavior is
      a no-op + reliance on :meth:`asyncio.Task.cancel`; subclasses
      override if they own resources (websockets, etc) that need
      explicit closing.

    The supervisor (in :class:`argosy.services.jobs.registry.JobRegistry`)
    owns the ``asyncio.Task`` that calls :meth:`run`, the exponential
    backoff state, and the ``job_runs`` audit rows.
    """

    #: Stable name; matches :class:`JobMetadata.name`.
    name: str = "base_longrunning"

    def __init__(self) -> None:
        # ``_exit_intent`` is the source of truth read by the supervisor
        # via the :attr:`exit_intent` property. Subclasses MAY set it
        # directly (the cleanest pattern is to set it in a ``finally``
        # block inside ``run()`` so it's always populated before return).
        self._exit_intent: ExitIntent = "unset"

    @abc.abstractmethod
    async def run(self) -> None:
        """Run the long-lived body. Set ``self._exit_intent`` before
        returning (or raising) so the supervisor can decide restart
        semantics. Raising propagates as ``exit_intent='crashed'`` from
        the supervisor's perspective.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def connection_status(self) -> ConnectionStatus:
        """Fast read; called by the registry every ~10s for UI status."""
        raise NotImplementedError

    async def cancel(self) -> None:
        """Optional async cancel hook called BEFORE the supervisor
        cancels the run-task.

        Default: no-op. Subclasses owning resources (e.g. an open
        websocket) override this to close them so :meth:`run` exits
        promptly. The supervisor follows up with ``task.cancel()`` if
        ``run()`` does not return within the supervisor's cancel
        timeout.
        """
        return None

    @property
    def exit_intent(self) -> ExitIntent:
        """Restart-decision source. Set by :meth:`run` (or by the
        supervisor's cancel path) BEFORE the supervisor inspects it.

        * ``"operator_stop"`` → no auto-restart.
        * ``"clean"``         → no auto-restart in v1 (spec §3,
                                IMPORTANT #3); operator reconnects manually.
        * ``"crashed"``       → exponential backoff + restart.
        * ``"unset"``         → defensive default; supervisor disambiguates
                                based on how ``run()`` exited (normal return
                                → coerced to ``"clean"``; raised exception
                                → coerced to ``"crashed"``).
        """
        return getattr(self, "_exit_intent", "unset")


__all__ = [
    "CadenceLoop",
    "ConnectionStatus",
    "ExitIntent",
    "LongRunningJob",
    "LoopSchedule",
    "TickStatus",
]
