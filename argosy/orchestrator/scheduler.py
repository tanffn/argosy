"""Async cadence scheduler (SDD §5).

Each registered `CadenceLoop` runs on its own coroutine. The scheduler
respects per-loop `enabled` and `market_hours_only` flags, persists
tick history in `cadence_state`, and reads schedule from the user's
`agent_settings.yaml` (writing a default when missing — see
`argosy.agent_settings.load_agent_settings`).

Phase 2 wires only the `daily_brief` loop. The architecture
accommodates the others (minute/hour/weekly/monthly/quarterly/annual)
without redesign — register them via `register_loop(...)` once
implemented.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select

from argosy.agent_settings import AgentSettings, load_agent_settings
from argosy.logging import get_logger
from argosy.orchestrator.loops.annual import AnnualLoop
from argosy.orchestrator.loops.audit import AuditLoop
from argosy.orchestrator.loops.backup import BackupLoop
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule, TickStatus
from argosy.orchestrator.loops.daily_brief import DailyBriefLoop
from argosy.orchestrator.loops.hour_loop import HourLoop
from argosy.orchestrator.loops.minute_loop import MinuteLoop
from argosy.orchestrator.loops.monthly_cycle import MonthlyCycleLoop
from argosy.orchestrator.loops.process_cooling import ProcessCoolingLoop
from argosy.orchestrator.loops.quarterly import QuarterlyLoop
from argosy.orchestrator.loops.plan_watcher import PlanWatcherLoop
from argosy.orchestrator.loops.watchlist import WatchlistLoop
from argosy.orchestrator.loops.weekly_review import WeeklyReviewLoop
from argosy.orchestrator.triggers import is_market_open
from argosy.state import db as db_mod
from argosy.state.models import CadenceState

_log = get_logger("argosy.scheduler")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    """Owns the loop registry, runs each loop on its own coroutine.

    `clock` is injectable for tests; the scheduler asks the clock both
    for the current time AND to advance time during its sleep step (the
    tests substitute a fake clock that resolves immediately).
    """

    def __init__(
        self,
        *,
        user_id: str = "ariel",
        settings: AgentSettings | None = None,
        clock: Callable[[], datetime] | None = None,
        market_open_check: Callable[[], bool] | None = None,
    ) -> None:
        self.user_id = user_id
        self.settings = settings or load_agent_settings(user_id)
        self.clock = clock or _utcnow
        self._market_open_check = market_open_check or (
            lambda: is_market_open(now=self.clock)
        )
        self._loops: dict[str, CadenceLoop] = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_loop(self, loop: CadenceLoop) -> None:
        """Register a single loop. The schedule is read from settings."""
        self._loops[loop.name] = loop

    def register_default_loops(self) -> None:
        """Register the Phase 2+3 default set.

        Phase 2: daily_brief.
        Phase 3: weekly_review (full T3-style plan-critique re-pass) +
        process_cooling (state-machine advancer for cooling proposals).
        Other loops (minute/hour/monthly/quarterly/annual) land in later
        phases as their tick implementations arrive.
        """
        cad = self.settings.cadences.daily_brief
        if cad.enabled:
            schedule = LoopSchedule.from_config(cad)
            self.register_loop(
                DailyBriefLoop(
                    schedule=schedule,
                    enabled=True,
                    user_id=self.user_id,
                )
            )

        weekly = self.settings.cadences.weekly_review
        if weekly.enabled:
            schedule = LoopSchedule.from_config(weekly)
            self.register_loop(
                WeeklyReviewLoop(
                    schedule=schedule,
                    enabled=True,
                    user_id=self.user_id,
                )
            )

        # Process-cooling runs every minute regardless of market hours;
        # it's a cheap DB scan with no LLM calls. We don't gate it on a
        # cadence config field (always-on for Phase 3).
        self.register_loop(
            ProcessCoolingLoop(
                schedule=LoopSchedule(interval_seconds=60),
                enabled=True,
                user_id=self.user_id,
                settings=self.settings,
            )
        )

        # Phase 4: ReconcileLoop polls open broker orders during market
        # hours and updates fills + pending_orders. Without this loop,
        # live orders would sit in pending_orders forever.
        try:
            from argosy.execution.reconcile import ReconcileLoop

            self.register_loop(
                ReconcileLoop(
                    schedule=LoopSchedule(
                        interval_seconds=30,
                        market_hours_only=True,
                    ),
                    enabled=True,
                    user_id=self.user_id,
                )
            )
        except ImportError:  # pragma: no cover - defensive
            pass

        # Phase 7: minute / hour / monthly / quarterly / annual / backup
        # All gated on `cadences.<name>.enabled` from agent_settings.yaml.
        cadences = self.settings.cadences

        if cadences.minute.enabled:
            self.register_loop(
                MinuteLoop(
                    schedule=LoopSchedule.from_config(cadences.minute),
                    enabled=True,
                    user_id=self.user_id,
                )
            )
        if cadences.hour.enabled:
            self.register_loop(
                HourLoop(
                    schedule=LoopSchedule.from_config(cadences.hour),
                    enabled=True,
                    user_id=self.user_id,
                )
            )
        if cadences.monthly_cycle.enabled:
            self.register_loop(
                MonthlyCycleLoop(
                    schedule=LoopSchedule.from_config(cadences.monthly_cycle),
                    enabled=True,
                    user_id=self.user_id,
                )
            )
        if cadences.quarterly.enabled:
            self.register_loop(
                QuarterlyLoop(
                    schedule=LoopSchedule.from_config(cadences.quarterly),
                    enabled=True,
                    user_id=self.user_id,
                )
            )
        if cadences.annual.enabled:
            self.register_loop(
                AnnualLoop(
                    schedule=LoopSchedule.from_config(cadences.annual),
                    enabled=True,
                    user_id=self.user_id,
                )
            )
        if cadences.backup.enabled:
            self.register_loop(
                BackupLoop(
                    schedule=LoopSchedule.from_config(cadences.backup),
                    enabled=True,
                    user_id=self.user_id,
                    settings=self.settings,
                )
            )
        if cadences.audit.enabled:
            self.register_loop(
                AuditLoop(
                    schedule=LoopSchedule.from_config(cadences.audit),
                    enabled=True,
                    user_id=self.user_id,
                )
            )
        if cadences.watchlist.enabled:
            self.register_loop(
                WatchlistLoop(
                    schedule=LoopSchedule.from_config(cadences.watchlist),
                    enabled=True,
                    user_id=self.user_id,
                )
            )
        if cadences.plan_watcher.enabled:
            self.register_loop(
                PlanWatcherLoop(
                    schedule=LoopSchedule.from_config(cadences.plan_watcher),
                    enabled=True,
                    user_id=self.user_id,
                )
            )

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Spawn one task per loop and block until `stop()` is called."""
        if not self._loops:
            self.register_default_loops()
        if not self._loops:
            _log.warning("scheduler.no_loops_registered")
            return
        tasks = [
            asyncio.create_task(self._run_loop(loop), name=f"cadence:{loop.name}")
            for loop in self._loops.values()
        ]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self) -> None:
        """Signal `run_forever` to wind down."""
        self._stop.set()

    # ------------------------------------------------------------------
    # Single-loop driver
    # ------------------------------------------------------------------

    async def _run_loop(self, loop: CadenceLoop) -> None:
        if not loop.enabled:
            _log.info("cadence.disabled", loop=loop.name)
            return
        while not self._stop.is_set():
            now = self.clock()
            next_due = loop.schedule.next_due_after(now)
            wait_secs = max(0.0, (next_due - now).total_seconds())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_secs)
                return  # stop signaled during wait
            except asyncio.TimeoutError:
                pass

            # Re-check enabled/market-hours at fire time.
            if not loop.enabled:
                continue
            if loop.schedule.market_hours_only and not self._market_open_check():
                await self._record_tick(loop.name, status=TickStatus.SKIPPED, error=None, next_due=next_due)
                continue

            await self._fire_once(loop)

    async def fire_once(self, loop_name: str) -> None:
        """One-shot: fire a registered loop now, regardless of schedule.

        Used by `argosy brief` and tests. Skips the market-hours guard so
        the user can trigger on demand.
        """
        loop = self._loops.get(loop_name)
        if loop is None:
            raise KeyError(f"loop {loop_name!r} not registered")
        await self._fire_once(loop, force=True)

    async def _fire_once(self, loop: CadenceLoop, *, force: bool = False) -> None:
        try:
            await loop.tick(now=self.clock)
        except Exception as exc:
            _log.exception("cadence.tick_failed", loop=loop.name)
            await self._record_tick(loop.name, status=TickStatus.ERROR, error=str(exc))
            if force:
                raise
            return
        await self._record_tick(loop.name, status=TickStatus.OK, error=None)

    async def _record_tick(
        self,
        loop_name: str,
        *,
        status: TickStatus,
        error: str | None,
        next_due: datetime | None = None,
    ) -> None:
        now = self.clock()
        try:
            async with db_mod.get_session() as session:
                row = (
                    await session.execute(
                        select(CadenceState).where(CadenceState.loop_name == loop_name)
                    )
                ).scalar_one_or_none()
                if row is None:
                    session.add(
                        CadenceState(
                            loop_name=loop_name,
                            last_tick_at=now,
                            next_due_at=next_due,
                            last_status=status.value,
                            last_error=error,
                        )
                    )
                else:
                    row.last_tick_at = now
                    if next_due is not None:
                        row.next_due_at = next_due
                    row.last_status = status.value
                    row.last_error = error
                await session.commit()
        except Exception:  # pragma: no cover - defensive: never let logging break the scheduler
            _log.exception("cadence.record_tick_failed", loop=loop_name)


__all__ = ["Scheduler"]
