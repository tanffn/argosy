"""Base abstractions for cadence loops (SDD §5)."""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

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


__all__ = ["CadenceLoop", "LoopSchedule", "TickStatus"]
