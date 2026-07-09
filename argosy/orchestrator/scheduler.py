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
from argosy.orchestrator.loops.hour_loop import HourLoop
from argosy.orchestrator.loops.minute_loop import MinuteLoop
from argosy.orchestrator.loops.monthly_cycle import MonthlyCycleLoop
from argosy.orchestrator.loops.process_cooling import ProcessCoolingLoop
from argosy.orchestrator.loops.quarterly import QuarterlyLoop
from argosy.orchestrator.loops.plan_watcher import PlanWatcherLoop
from argosy.orchestrator.loops.speculative_monitor_loop import SpeculativeMonitorLoop
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
        # Boot-time missed-run catch-ups run SEQUENTIALLY through this
        # gate — a server that was down across several cron fire-times
        # must not stampede every missed job (each an LLM-heavy run) at
        # once on restart.
        self._catchup_gate = asyncio.Semaphore(1)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_loop(self, loop: CadenceLoop) -> None:
        """Register a single loop. The schedule is read from settings."""
        self._loops[loop.name] = loop

    def register_default_loops(self) -> None:
        """Register the Phase 2+3 default set.

        Phase 2: daily_brief — RETIRED W9. The four-agent
        ``DailyBriefLoop`` (news + macro + concentration + plan_critique
        composer) is gone; the canonical path is now T4.5's
        single-agent runner in ``argosy/services/daily_brief_runner.py``,
        which the ``argosy brief`` CLI invokes directly. The
        ``cadences.daily_brief`` settings field is preserved for
        backwards-compat but no longer wires a scheduler loop.
        Phase 3: weekly_review (full T3-style plan-critique re-pass) +
        process_cooling (state-machine advancer for cooling proposals).
        Other loops (minute/hour/monthly/quarterly/annual) land in later
        phases as their tick implementations arrive.
        """
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

        # S18: daily stop-loss / sell-signal sweep over the high-risk
        # speculative single names (the user's "live daily monitor" ask).
        # Always-on like process_cooling — no cadence settings field; cheap
        # (a handful of yfinance reads in a worker thread).
        self.register_loop(
            SpeculativeMonitorLoop(
                schedule=LoopSchedule(interval_seconds=86_400),
                enabled=True,
                user_id=self.user_id,
            )
        )

        # Discovery funnel — a SEPARATE daily loop (codex #10): heavier
        # radar->estimator->fleet pass with its own cadence + failure isolation,
        # kept apart from the cheap speculative monitor. Smart refresh.
        # Cron slot (16:00 Asia/Jerusalem) lives in the loop's default
        # schedule — an interval here re-anchored on every restart and
        # starved the loop.
        from argosy.orchestrator.loops.discovery_funnel_loop import (
            DiscoveryFunnelLoop,
        )

        self.register_loop(
            DiscoveryFunnelLoop(
                enabled=True,
                user_id=self.user_id,
            )
        )

        # Daily FX refresh — keep USD/NIS fresh for ALL consumers (retirement MC,
        # dashboards, TSV), not just the on-demand deploy/directive path. Always-on
        # like the monitors; cheap + failure-isolated.
        from argosy.orchestrator.loops.fx_refresh_loop import FxRefreshLoop

        self.register_loop(
            FxRefreshLoop(
                schedule=LoopSchedule(interval_seconds=86_400),
                enabled=True,
                user_id=self.user_id,
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

        # T5.6 — StateObserverLoop. Gated on
        # ``cadences.state_observer.enabled`` (default True). Mirrors
        # the identical block in argosy/api/main.py so that `argosy run`
        # boots the same job set as the FastAPI server.
        try:
            from argosy.orchestrator.loops.state_observer import (  # noqa: PLC0415
                StateObserverLoop,
            )

            obs_cfg = cadences.state_observer
            if obs_cfg.enabled:
                self.register_loop(
                    StateObserverLoop(
                        schedule=LoopSchedule.from_config(obs_cfg),
                        enabled=True,
                        user_id=self.user_id,
                    )
                )
        except (ImportError, ValueError) as exc:
            _log.exception(
                "scheduler.state_observer_register_failed",
                error_type=type(exc).__name__,
            )

        # T5.6 — PredictionsEvaluatorLoop. Gated on
        # ``cadences.predictions_evaluator.enabled`` (default True).
        # Mirrors the identical block in argosy/api/main.py so that
        # `argosy run` boots the same job set as the FastAPI server.
        try:
            from argosy.orchestrator.loops.predictions_evaluator import (  # noqa: PLC0415
                PredictionsEvaluatorLoop,
            )

            pe_cfg = cadences.predictions_evaluator
            if pe_cfg.enabled:
                self.register_loop(
                    PredictionsEvaluatorLoop(
                        schedule=LoopSchedule.from_config(pe_cfg),
                        enabled=True,
                    )
                )
        except (ImportError, ValueError) as exc:
            _log.exception(
                "scheduler.predictions_evaluator_register_failed",
                error_type=type(exc).__name__,
            )

        # PayslipIngestLoop — daily §102 RSU-withholding closed loop. Mirrors
        # the block in argosy/api/main.py so `argosy run` boots the same job set.
        try:
            from argosy.orchestrator.loops.payslip_ingest import (  # noqa: PLC0415
                PayslipIngestLoop,
            )

            self.register_loop(
                PayslipIngestLoop(enabled=True, user_id=self.user_id)
            )
        except (ImportError, ValueError) as exc:
            _log.exception(
                "scheduler.payslip_ingest_register_failed",
                error_type=type(exc).__name__,
            )

        # HolisticRebalanceReviewLoop — quarterly whole-portfolio rebalance
        # review. Self-contained quarterly cron (no cadence config field, like
        # thesis_monitor). Mirrors the identical block in argosy/api/main.py so
        # `argosy run` boots the same job set as the FastAPI server.
        try:
            from argosy.orchestrator.loops.holistic_rebalance_review import (  # noqa: PLC0415
                HolisticRebalanceReviewLoop,
            )

            self.register_loop(
                HolisticRebalanceReviewLoop(
                    enabled=True,
                    user_id=self.user_id,
                )
            )
        except (ImportError, ValueError) as exc:
            _log.exception(
                "scheduler.holistic_rebalance_review_register_failed",
                error_type=type(exc).__name__,
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
        await self._catch_up_if_missed(loop)
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

    async def _catch_up_if_missed(self, loop: CadenceLoop) -> None:
        """Boot-time catch-up: if this loop's most recent scheduled fire
        was missed (no tick recorded at or after it — the server was down),
        fire it now instead of silently waiting for the next cron slot.

        A daily review that runs at 17:00 must not be lost to a 16:55-to-
        17:05 server restart, and a server that was down all day must run
        the day's reviews when it comes back — the daily pipeline IS the
        product (proactive agency: the client never asks).

        Scope guards:
          * cron-driven loops only (``prev_due_before`` returns None
            otherwise) — interval loops re-fire within one interval anyway;
          * a loop that has NEVER ticked also catches up (a newly shipped
            daily job's first fire must not depend on the server being up
            at exactly its cron time);
          * ``market_hours_only`` loops respect the market gate, same as a
            scheduled fire;
          * catch-ups run sequentially through ``_catchup_gate`` (no boot
            stampede when several jobs were missed);
          * kill switch: ``scheduler_catchup_on_boot`` (default on).
        """
        try:
            from argosy.config import get_settings

            if not getattr(get_settings(), "scheduler_catchup_on_boot", True):
                return
        except Exception:  # pragma: no cover - settings must never break boot
            pass
        prev_due = loop.schedule.prev_due_before(self.clock())
        if prev_due is None:
            return
        # Only catch up RECENTLY missed fires. A long-idle slot (e.g. the
        # annual loop's January 2nd, months stale by the time a never-run
        # loop is first adopted) firing wildly out of season is noise, not
        # a recovered review — it waits for its next scheduled slot or a
        # manual Run-now.
        try:
            from argosy.config import get_settings as _gs

            _max_age_days = float(
                getattr(_gs(), "scheduler_catchup_max_age_days", 7.0)
            )
        except Exception:  # pragma: no cover
            _max_age_days = 7.0
        if (self.clock() - prev_due).total_seconds() > _max_age_days * 86400:
            _log.info(
                "cadence.catchup_skipped_too_stale",
                loop=loop.name,
                missed_due=prev_due.isoformat(),
                max_age_days=_max_age_days,
            )
            return
        last_tick_at: datetime | None = None
        try:
            async with db_mod.get_session() as session:
                row = (
                    await session.execute(
                        select(CadenceState).where(
                            CadenceState.loop_name == loop.name
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    last_tick_at = row.last_tick_at
        except Exception:  # pragma: no cover - a broken read must not block the loop
            _log.exception("cadence.catchup_state_read_failed", loop=loop.name)
            return
        if last_tick_at is not None:
            if last_tick_at.tzinfo is None:
                last_tick_at = last_tick_at.replace(tzinfo=timezone.utc)
            if last_tick_at >= prev_due:
                return  # the scheduled fire happened (or was attempted)
        async with self._catchup_gate:
            if self._stop.is_set():
                return
            if loop.schedule.market_hours_only and not self._market_open_check():
                _log.info(
                    "cadence.catchup_skipped_market_closed", loop=loop.name,
                )
                return
            _log.info(
                "cadence.catchup_fire",
                loop=loop.name,
                missed_due=prev_due.isoformat(),
                last_tick_at=(
                    last_tick_at.isoformat() if last_tick_at else None
                ),
            )
            try:
                await self._fire_once(loop)
            except Exception:  # noqa: BLE001
                # `Scheduler._fire_once` already absorbs tick exceptions,
                # but subclass overrides can raise BEFORE the tick (e.g.
                # RegisteredScheduler fails fast on a loop the registry
                # rejected). A failed catch-up must never kill this loop's
                # cadence task — the regular schedule continues.
                _log.exception("cadence.catchup_fire_failed", loop=loop.name)

    async def fire_once(self, loop_name: str) -> None:
        """One-shot: fire a registered loop now, regardless of schedule.

        Used by `argosy brief` and tests. Skips the market-hours guard so
        the user can trigger on demand.
        """
        loop = self._loops.get(loop_name)
        if loop is None:
            raise KeyError(f"loop {loop_name!r} not registered")
        await self._fire_once(loop, force=True)

    async def _fire_once(
        self, loop: CadenceLoop, *, force: bool = False
    ) -> dict | None:
        """Fire a single tick. Returns the tick's ``output_summary`` dict
        (or ``None``) so subclasses — notably
        :class:`RegisteredScheduler` (Spec A commit #7) — can capture it
        and persist as ``job_runs.output_summary``. Plain :class:`Scheduler`
        ignores the return value; the audit-row writeback lives in the
        registry subclass.

        Spec A commit #7 widened the contract: tick's return value is
        now propagated through ``_fire_once`` rather than discarded.
        Pre-#7 callers passed ``None`` from every loop, so the type
        widening is backwards-compatible.
        """
        try:
            result = await loop.tick(now=self.clock)
        except Exception as exc:
            _log.exception("cadence.tick_failed", loop=loop.name)
            await self._record_tick(loop.name, status=TickStatus.ERROR, error=str(exc))
            if force:
                raise
            return None
        await self._record_tick(loop.name, status=TickStatus.OK, error=None)
        return result

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
