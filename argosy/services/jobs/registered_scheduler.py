"""``RegisteredScheduler`` — the registry's composition seam over ``Scheduler``.

Spec A §1.6 (codex BLOCKER #5) replaced an earlier draft that proposed
``pre_tick`` / ``post_tick`` callable slots on :class:`Scheduler` with
explicit subclass composition. ``Scheduler`` stays unchanged; this
module subclasses it and overrides exactly one method
(``_fire_once``).

Ordering invariant (spec §1.7 — codex BLOCKER #4): inside the tick
body, the order of writes is

1. ``_open_job_run`` (registry — audit row created in ``status='running'``)
2. ``await loop.tick(...)`` (the actual work)
3. ``_close_job_run`` (registry — audit row finalized to ``ok`` / ``error``)
4. ``_record_tick`` (parent — ``cadence_state`` updated)

Step 4 is ATTEMPTED EVEN IF step 3 fails — per spec §1.7 the matrix
row "close fails, record_tick ok" must be reachable. Close-failure is
logged and the cadence pointer write proceeds, leaving an audit row
stuck in ``running`` (reaped by the retention loop in commit #9).

Lock acquisition (§1.4) — single-acquire model (closes round-2 codex
BLOCKER on the release-then-reacquire race):

* :meth:`_fire_once` (scheduled-path entry) acquires the per-job lock
  and calls :meth:`fire_once_already_locked` with
  ``manual_trigger=False``, ``triggered_by="scheduler"``.
* :meth:`JobRegistry.fire_now` (manual-path entry) acquires the lock
  itself with a 1s timeout, then calls
  :meth:`fire_once_already_locked` DIRECTLY with the supplied
  ``manual_trigger=True`` / ``triggered_by=<label>``. The lock is
  released only in the manual path's ``finally`` block after the
  dispatch returns — no release-then-reacquire window exists.

The ``manual_trigger`` / ``triggered_by`` labels flow through the
call chain as parameters; no shared mutable state.

The override REIMPLEMENTS the parent's ``_fire_once`` body rather than
delegating to ``super()._fire_once`` because the parent runs
``_record_tick`` at the end of its own body — we need ``_close_job_run``
to land BEFORE ``_record_tick`` so that the §1.7 failure matrix holds.

To keep "single code path" honest: the tick coroutine call here is
``await loop.tick(now=self.clock)`` — the IDENTICAL call the parent
makes at ``scheduler.py:282``. If the parent's tick body ever grows
extra steps (pre-flight checks, post-flight hooks), this override
must be updated to match. ``test_fire_once_matches_parent_body``
asserts structural parity by comparing line counts + key tokens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, TickStatus
from argosy.orchestrator.scheduler import Scheduler

if TYPE_CHECKING:  # pragma: no cover
    from argosy.services.jobs.registry import JobRegistry

_log = get_logger("argosy.jobs.registered_scheduler")


class RegisteredScheduler(Scheduler):
    """Scheduler variant that records every ``_fire_once`` into ``job_runs``.

    The override is intentionally small + visible: subclass-typed, not
    callable-injected (codex BLOCKER #5). The ``Scheduler`` parent is
    untouched.

    The registry must be wired BEFORE :meth:`Scheduler.run_forever` is
    invoked. In tests this means constructing the scheduler with
    ``registry=…`` (see fixtures in ``tests/test_jobs_registry.py``);
    in production commit #3b sets it up inside ``create_app``'s startup
    hook.
    """

    def __init__(self, *args, registry: "JobRegistry", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._registry = registry

    async def _fire_once(
        self, loop: CadenceLoop, *, force: bool = False
    ) -> dict | None:
        """Override of :meth:`Scheduler._fire_once` — scheduled-path entry.

        Returns ``None`` — the tick's output_summary is captured
        internally by :meth:`fire_once_already_locked` and persisted to
        ``job_runs.output_summary`` via :meth:`_close_safely`. The
        return-type annotation matches the parent's widened
        ``dict | None`` (Spec A commit #7) but the subclass intentionally
        always returns ``None`` because the audit-row writeback is the
        load-bearing channel, not the function return value.

        This is the lock-acquiring entry. The manual path
        (:meth:`JobRegistry.fire_now`) calls
        :meth:`fire_once_already_locked` directly with the lock already
        held — that's the single-acquire model that closes the
        round-2 codex BLOCKER ("release-then-reacquire could mislabel
        audit rows").

        IMPORTANT: this method (along with the manual-path entry) is
        the ONLY caller of ``_open_job_run`` / ``_close_job_run`` for
        ``CadenceLoop`` ticks. The ``LongRunningJob`` supervisor
        (commit #5) is the only other caller; together they are the
        two paths the §1.3 single-writer contract covers.
        """
        # The registry MUST own the per-job lock; if it doesn't, the
        # registry + scheduler were wired without co-registering the
        # loop. That's a programmer error and we fail fast (codex
        # IMPORTANT round-3 — don't silently bypass the lock in
        # production). Tests that need to bypass the registry build a
        # bare ``Scheduler`` instead of a ``RegisteredScheduler``.
        try:
            lock = self._registry._lock_for(loop.name)
        except KeyError as exc:
            raise RuntimeError(
                f"RegisteredScheduler._fire_once: loop {loop.name!r} is "
                "not registered with the JobRegistry — registry + "
                "scheduler must co-register every loop. Use a bare "
                "Scheduler() for tests that bypass the registry."
            ) from exc

        # Scheduled path: triggered_by defaults to "scheduler".
        # The manual entry (JobRegistry.fire_now) calls
        # fire_once_already_locked directly with manual_trigger=True.
        async with lock:
            await self.fire_once_already_locked(
                loop,
                force=force,
                manual_trigger=False,
                triggered_by="scheduler",
            )

    async def fire_once_already_locked(
        self,
        loop: CadenceLoop,
        *,
        force: bool,
        manual_trigger: bool,
        triggered_by: str,
    ) -> None:
        """Tick body, called with the per-job lock already held.

        Called from two places:
          - :meth:`_fire_once` (scheduled path) after acquiring the lock.
          - :meth:`JobRegistry.fire_now` (manual path) after its own
            acquire-with-timeout succeeds.

        The single-acquire model (no release-then-reacquire) ensures
        the audit row's ``manual_trigger`` / ``triggered_by`` cannot be
        misattributed by a racing scheduled tick (closes the round-2
        codex BLOCKER).

        Step 1 — audit row opened in 'running'.
        Step 2 — ``await loop.tick(now=self.clock)`` (single code path
                 — mirrors Scheduler._fire_once body at scheduler.py:282).
        Step 3 — close audit. On failure, log + continue to step 4 per
                 the §1.7 matrix row "close fails, record_tick ok".
        Step 4 — parent's cadence_state pointer write.
        """
        try:
            run_id = await self._registry._open_job_run(
                job_name=loop.name,
                manual_trigger=manual_trigger,
                triggered_by=triggered_by,
            )
        except Exception:  # pragma: no cover - DB unavailable path
            # Spec §1.7 final row: open-fail → log + skip this tick.
            _log.exception(
                "jobs.open_job_run_failed",
                loop=loop.name,
                triggered_by=triggered_by,
            )
            if force:
                raise
            return

        try:
            tick_result = await loop.tick(now=self.clock)
        except Exception as exc:
            _log.exception("cadence.tick_failed", loop=loop.name)
            # Step 3 (error) — close BEFORE step 4 (ordering invariant).
            # Spec A commit #7: even on the exception path we capture
            # `loop.last_output_summary` so multi-stage jobs (e.g.
            # NewsDailyJob — Stage 1 ingest + Stage 2 analyst) can
            # surface partial progress when Stage 2 raises. The job's
            # `tick()` sets this in a `finally` so it's populated before
            # the exception unwinds past us.
            await self._close_safely(
                run_id,
                status="error",
                error_message=str(exc),
                output_summary=_safe_output_summary(loop),
            )
            # Step 4 — parent's pointer write happens EVEN IF close
            # failed, so the matrix's "close fails, record_tick ok"
            # row is reachable. cadence_state ending fresh while audit
            # is stuck in 'running' is the trade-off documented in
            # §1.7; retention reaps the orphan.
            await self._record_tick(
                loop.name,
                status=TickStatus.ERROR,
                error=str(exc),
            )
            if force:
                raise
            return

        # Step 3 (ok) — close BEFORE step 4.
        # Spec A commit #7: prefer the tick's explicit return value
        # when present; fall back to `loop.last_output_summary` for
        # legacy loops that still use the attribute side-channel.
        # `_safe_output_summary` handles the None / non-dict coercion.
        output_summary = (
            tick_result
            if isinstance(tick_result, dict)
            else _safe_output_summary(loop)
        )
        await self._close_safely(
            run_id,
            status="ok",
            output_summary=output_summary,
        )
        # Step 4 — parent's pointer write (success branch). Runs even
        # if step 3 raised.
        await self._record_tick(loop.name, status=TickStatus.OK, error=None)

    async def _close_safely(
        self,
        run_id: int,
        *,
        status,
        error_message=None,
        output_summary=None,
    ) -> None:
        """Wrap ``_close_job_run`` so a close-write failure does not
        block the cadence_state pointer write (spec §1.7).

        On exception we log + swallow; the audit row stays in
        'running' until the retention loop reaps it.
        """
        try:
            await self._registry._close_job_run(
                run_id,
                status=status,
                error_message=error_message,
                output_summary=output_summary,
            )
        except Exception:  # pragma: no cover - DB unavailable path
            _log.exception(
                "jobs.close_job_run_failed",
                run_id=run_id,
                intended_status=status,
            )


def _safe_output_summary(loop: CadenceLoop):
    """Read ``loop.last_output_summary`` defensively.

    Spec §1.7 / commit #7 widens ``CadenceLoop.tick`` to return
    ``dict | None`` — the registry now captures the return value
    directly on the success path. This attribute is the FALLBACK:

    * On the exception path: ``tick()`` may have set
      ``self.last_output_summary`` in a ``finally`` block to surface
      per-stage partial progress (codex NICE #7). The adapter reads it
      here to capture e.g. "Stage 1 ingested ok, Stage 2 raised" even
      though no return value made it back.
    * Legacy loops that never set ``last_output_summary``: the attribute
      is missing → returns ``None`` → stored as NULL.
    """
    summary = getattr(loop, "last_output_summary", None)
    if summary is None:
        return None
    if isinstance(summary, dict):
        return summary
    # Coerce non-dict summaries into the documented shape so the
    # ``json_valid`` CHECK on the DB column doesn't reject.
    return {"notes": str(summary)}


__all__ = ["RegisteredScheduler"]
