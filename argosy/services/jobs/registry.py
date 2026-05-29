"""``JobRegistry`` — single-writer audit recorder for the in-process scheduler.

Spec A — ``docs/superpowers/specs/2026-05-29-jobs-registry-design.md``,
commit #3a. The shell only; lifecycle wiring lives in #3b.

The registry holds:

* Per-job :class:`asyncio.Lock` (§1.4) keyed by ``job.name``. The
  scheduled-path :class:`RegisteredScheduler` acquires the lock around
  every ``_fire_once``; manual-path :meth:`JobRegistry.fire_now` tries
  to acquire with a 1-second timeout (per-job configurable via
  :attr:`JobMetadata.lock_acquire_timeout_s`).
* :meth:`_open_job_run` / :meth:`_close_job_run` — the ONLY helpers that
  write to the ``job_runs`` table. Codex BLOCKER #2 — the single-writer
  contract is asserted by ``test_jobs_registry.test_job_runs_single_writer``.
* In-memory ``_lock_holders`` map tracking the currently-holding
  ``job_run_id`` per job for the 409 contention shape (codex IMPORTANT
  #1 — ``lock_holder_state`` is one of ``"running" | "starting" |
  "unknown"``; ``job_run_id`` is nullable).
* :func:`_derive_health` — server-side health derivation per §1.6 table.

The class is constructible TODAY but does not auto-run anything. The
FastAPI startup hook in commit #3b instantiates it after constructing
``RegisteredScheduler`` and calling ``register_default_loops()``.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LongRunningJob
from argosy.state import db as db_mod
from argosy.state.models import CadenceState, JobRun

if TYPE_CHECKING:  # pragma: no cover
    from argosy.orchestrator.scheduler import Scheduler

_log = get_logger("argosy.jobs.registry")

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

SourceKind = Literal["ingest", "monitor", "maintenance", "notification"]
LockHolderState = Literal["running", "starting", "unknown"]
Health = Literal["green", "amber", "red", "unknown"]


@dataclass(frozen=True)
class JobMetadata:
    """Static description of a registered job — surfaced by ``GET /api/jobs``."""

    name: str
    schedule_cron: str | None
    schedule_human: str
    source_kind: SourceKind
    description: str
    long_running: bool = False
    #: Per-job lock-acquire timeout (codex NICE #1). Default 1.0s.
    lock_acquire_timeout_s: float = 1.0


@dataclass
class JobView:
    """Computed view returned by :meth:`JobRegistry.list` / ``GET /api/jobs``."""

    metadata: JobMetadata
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    last_run_error: str | None = None
    next_run_at: datetime | None = None
    currently_running_run_id: int | None = None
    health: Health = "unknown"


class AlreadyRunning(Exception):
    """Raised by :meth:`JobRegistry.fire_now` when the per-job lock is held.

    Mirrors the 409 response body documented in §1.4. ``job_run_id`` is
    nullable: the in-memory holder map may be stale if the prior holder
    crashed before releasing — the route surfaces that as
    ``lock_holder_state="unknown"`` with a null ``job_run_id``.
    """

    def __init__(
        self,
        *,
        job_name: str,
        job_run_id: int | None,
        lock_holder_state: LockHolderState,
        lock_acquired_at: datetime | None,
        retry_after_s: int = 5,
    ) -> None:
        super().__init__(
            f"job {job_name!r} already running "
            f"(holder={lock_holder_state}, run_id={job_run_id})"
        )
        self.job_name = job_name
        self.job_run_id = job_run_id
        self.lock_holder_state = lock_holder_state
        self.lock_acquired_at = lock_acquired_at
        self.retry_after_s = retry_after_s

    def to_response_body(self) -> dict[str, Any]:
        """Serialize to the §1.4 / §8 409 response shape."""
        return {
            "error": "already_running",
            "conflict_reason": "lock_held",
            "job_name": self.job_name,
            "job_run_id": self.job_run_id,
            "lock_holder_state": self.lock_holder_state,
            "lock_acquired_at": (
                self.lock_acquired_at.isoformat()
                if self.lock_acquired_at is not None
                else None
            ),
            "retry_after_s": self.retry_after_s,
        }


# ---------------------------------------------------------------------------
# Internal book-keeping
# ---------------------------------------------------------------------------


@dataclass
class _LockHolder:
    """In-memory marker for who currently holds a job's lock."""

    job_run_id: int | None
    state: LockHolderState
    acquired_at: datetime


@dataclass
class _RegisteredJob:
    """Internal record. Public surface is :class:`JobView` via :meth:`list`."""

    metadata: JobMetadata
    # Either a :class:`CadenceLoop` (scheduled-path) or a
    # :class:`LongRunningJob` (supervised-path). The discriminator is
    # ``metadata.long_running``, kept in sync by :meth:`JobRegistry.register`.
    job: "CadenceLoop | LongRunningJob"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ---------------------------------------------------------------------------
# Idempotency-key helper (kept module-level so tests can call it directly)
# ---------------------------------------------------------------------------


def _idempotency_key(job_name: str, started_at: datetime, triggered_by: str) -> str:
    """Spec §1.7 formula: ``{job_name}|{iso-seconds-started_at}|{triggered_by}``.

    ``started_at`` is coerced to UTC before formatting so the key is
    stable regardless of caller TZ.
    """
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    iso = started_at.astimezone(timezone.utc).isoformat(timespec="seconds")
    return f"{job_name}|{iso}|{triggered_by}"


# ---------------------------------------------------------------------------
# Health derivation (§1.6 table)
# ---------------------------------------------------------------------------


def _derive_health(view: JobView, *, cadence_seconds: float | None = None) -> Health:
    """Server-side health derivation per spec §1.6.

    Caller passes the loop's interval-or-cron-derived cadence in
    seconds when known; for ``LongRunningJob`` (commit #5) the
    connection-status branch takes precedence and ``cadence_seconds``
    is irrelevant.
    """
    status = view.last_run_status
    now = datetime.now(timezone.utc)

    # LongRunningJob branch — connection_status() is the source of truth.
    if status in ("connected",):
        return "green"
    if status in ("reconnecting",):
        if view.last_run_at is None:
            return "amber"
        age_s = (now - _ensure_utc(view.last_run_at)).total_seconds()
        return "amber" if age_s < 60 else "red"
    if status in ("stopped",):
        return "red"

    if status == "error":
        return "red"
    if status == "skipped":
        return "green"
    if status == "cancelled":
        # Cancelled = reaped orphan or operator-stop; surface as amber so
        # the operator sees it without alarm.
        return "amber"

    if status == "running":
        if view.last_run_at is None:
            return "amber"
        age_s = (now - _ensure_utc(view.last_run_at)).total_seconds()
        return "green" if age_s < 600 else "amber"

    if status == "ok":
        if cadence_seconds is None or view.last_run_at is None:
            return "green"
        age_s = (now - _ensure_utc(view.last_run_at)).total_seconds()
        return "green" if age_s < 2 * cadence_seconds else "amber"

    return "unknown"


def _ensure_utc(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC (the project convention)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# JobRegistry
# ---------------------------------------------------------------------------


class JobRegistry:
    """The shell. See module docstring for the spec contract.

    Lifecycle: a single instance per process. Constructed in
    ``argosy/api/main.py::create_app`` (commit #3b); tests construct
    their own throwaway instances.

    The registry can be constructed BEFORE the :class:`Scheduler`
    instance it composes with is finalized — tests use this to attach a
    :class:`RegisteredScheduler` afterwards. Production wires both
    together in one shot (#3b).
    """

    def __init__(self, scheduler: "Scheduler | None" = None) -> None:
        self._scheduler: "Scheduler | None" = scheduler
        self._jobs: dict[str, _RegisteredJob] = {}
        self._lock_holders: dict[str, _LockHolder] = {}
        self._log = _log
        # ------------------------------------------------------------
        # Supervisor state (commit #5 — LongRunningJob branch).
        # ------------------------------------------------------------
        #: Per-supervisor epoch — included in ``triggered_by`` so the
        #: idempotency_key (and the cycle-recovery lookup) is unique
        #: across supervisor task lifetimes. Without this, a fresh
        #: supervisor starting cycle=1 after a prior supervisor's
        #: cycle=1 would collide in the lookup-by-triggered_by fallback
        #: (codex round-3 BLOCKER #2).
        self._supervisor_epoch = 0
        #: Per-job supervisor :class:`asyncio.Task`. Populated by
        #: :meth:`start_supervisors`; cleared by :meth:`stop_supervisors`.
        self._supervisor_tasks: dict[str, asyncio.Task[None]] = {}
        #: Event that signals supervisors to exit their restart loop.
        #: Lazily constructed on first :meth:`start_supervisors` call so
        #: registries built outside an event loop don't immediately bind
        #: to the wrong loop.
        self._stop_supervisors: asyncio.Event | None = None
        #: Injected sleep — overridable for tests that need a fake clock.
        #: Tests set this on the instance to a coroutine factory that
        #: returns immediately, eliminating real wall-time waits.
        self._sleep = asyncio.sleep
        #: Bounds the exp-backoff supervisor restart delay. The Sprint
        #: A commit #5 prompt pins this at 60s ("1s → 2s → 4s → ...
        #: capped at 60s"); earlier spec drafts (§3 line ~339 and §5
        #: line ~581 in the spec doc) showed 300s and were superseded
        #: by the commit-#5 prompt. The 60s ceiling prevents runaway
        #: memory growth from a job that crashes on every restart
        #: (codex review focus #2).
        self._backoff_cap_s = 60.0
        #: Initial backoff after the first crash (doubles on each
        #: subsequent crash within the same restart cycle).
        self._backoff_initial_s = 1.0
        #: Timeout for :meth:`stop_supervisors` per-task await before
        #: forcing :meth:`asyncio.Task.cancel`.
        self._stop_supervisors_timeout_s = 5.0

    # ------------------------------------------------------------------
    # Scheduler binding
    # ------------------------------------------------------------------

    def bind_scheduler(self, scheduler: "Scheduler") -> None:
        """Late-bind the scheduler. Used by tests + commit #3b startup."""
        self._scheduler = scheduler

    @property
    def scheduler(self) -> "Scheduler | None":
        return self._scheduler

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        *,
        job: "CadenceLoop | LongRunningJob",
        metadata: JobMetadata,
    ) -> None:
        """Register a job + its metadata.

        Accepts either a :class:`CadenceLoop` (scheduled-path) or a
        :class:`LongRunningJob` (supervised-path; commit #5). The
        ``metadata.long_running`` discriminator MUST match the
        runtime type — mismatches are a programmer error and raise
        ``ValueError``.
        """
        if metadata.name != job.name:
            raise ValueError(
                f"metadata.name ({metadata.name!r}) must match job.name "
                f"({job.name!r})"
            )
        if metadata.name in self._jobs:
            raise ValueError(f"job {metadata.name!r} already registered")

        # Cross-check the discriminator: ``metadata.long_running`` MUST
        # agree with the runtime class so the scheduled-path vs.
        # supervisor-path dispatch can rely on either side.
        is_long_running = isinstance(job, LongRunningJob)
        if metadata.long_running != is_long_running:
            raise ValueError(
                f"metadata.long_running ({metadata.long_running!r}) "
                f"disagrees with job type ({type(job).__name__!r}). "
                f"Set long_running=True for LongRunningJob subclasses."
            )

        self._jobs[metadata.name] = _RegisteredJob(metadata=metadata, job=job)
        self._log.info(
            "jobs.registered",
            job=metadata.name,
            source_kind=metadata.source_kind,
            long_running=metadata.long_running,
        )

    def names(self) -> list[str]:
        return list(self._jobs.keys())

    def get_job(self, name: str) -> "CadenceLoop | LongRunningJob":
        """Return the registered job instance (or raise KeyError)."""
        return self._jobs[name].job

    def get_metadata(self, name: str) -> JobMetadata:
        return self._jobs[name].metadata

    def _lock_for(self, name: str) -> asyncio.Lock:
        """Return the per-job lock (or raise KeyError)."""
        return self._jobs[name].lock

    # ------------------------------------------------------------------
    # View
    # ------------------------------------------------------------------

    async def list(self) -> list[JobView]:
        """Materialize a snapshot of all registered jobs.

        Reads ``cadence_state`` for ``last_*`` fields; queries the
        ``job_runs`` table for the currently-running row id if any.
        This is the GET /api/jobs payload (commit #4).

        For :class:`LongRunningJob` instances, ``last_run_status`` is
        derived from :meth:`LongRunningJob.connection_status` (spec §3:
        "reflects connection health, not did-the-most-recent-run()-return-ok").
        The :func:`_derive_health` table has the matching branches for
        ``connected | reconnecting | stopped``.
        """
        out: list[JobView] = []
        async with db_mod.get_session() as session:
            for name, rec in self._jobs.items():
                cad = (
                    await session.execute(
                        select(CadenceState).where(
                            CadenceState.loop_name == name
                        )
                    )
                ).scalar_one_or_none()
                running_id = (
                    await session.execute(
                        select(JobRun.id)
                        .where(
                            (JobRun.job_name == name)
                            & (JobRun.status == "running")
                        )
                        .order_by(JobRun.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                view = JobView(
                    metadata=rec.metadata,
                    last_run_at=cad.last_tick_at if cad else None,
                    last_run_status=cad.last_status if cad else None,
                    last_run_error=cad.last_error if cad else None,
                    next_run_at=cad.next_due_at if cad else None,
                    currently_running_run_id=running_id,
                )

                # LongRunningJob branch: override last_run_status with
                # the job's live connection_status. For the
                # 'reconnecting > 60s' health-red boundary, also stamp
                # last_run_at to the most-recent running row's
                # started_at if available — that's when the current
                # reconnect cycle began.
                if isinstance(rec.job, LongRunningJob):
                    try:
                        view.last_run_status = rec.job.connection_status()
                    except Exception:  # pragma: no cover - defensive
                        view.last_run_status = "stopped"
                    if running_id is not None:
                        started = (
                            await session.execute(
                                select(JobRun.started_at).where(
                                    JobRun.id == running_id
                                )
                            )
                        ).scalar_one_or_none()
                        if started is not None:
                            view.last_run_at = started

                view.health = _derive_health(
                    view,
                    cadence_seconds=_cadence_seconds(rec.job),
                )
                out.append(view)
        return out

    async def get(self, name: str) -> JobView:
        """Single-job view. Raises KeyError if unknown."""
        if name not in self._jobs:
            raise KeyError(name)
        for view in await self.list():
            if view.metadata.name == name:
                return view
        # Unreachable — name was in self._jobs at the top.
        raise KeyError(name)  # pragma: no cover

    # ------------------------------------------------------------------
    # Manual trigger (spec §1.3 / §1.4)
    # ------------------------------------------------------------------

    async def fire_now(self, name: str, *, triggered_by: str = "user") -> int:
        """Manually fire a registered job. Returns the ``job_runs.id``.

        Implementation contract (§1.3 / §1.4 — addresses round-2 codex
        BLOCKER on the release-then-reacquire race):

        1. Acquire the per-job lock with the metadata's configured
           timeout. On timeout: raise :class:`AlreadyRunning` with the
           current holder's ``job_run_id`` (if known) and
           ``lock_holder_state`` (``"running" | "starting" | "unknown"``).
        2. HOLD the lock for the entire dispatch. Call
           ``RegisteredScheduler.fire_once_already_locked`` directly —
           bypassing the scheduled-path lock acquire — so there is no
           release-then-reacquire window. This guarantees the
           ``manual_trigger`` / ``triggered_by`` labels on the audit
           row cannot be misattributed to a racing scheduled tick.

        The manual entrypoint deliberately does NOT route through
        ``scheduler.fire_once`` (which goes through
        ``Scheduler._fire_once → RegisteredScheduler._fire_once`` and
        would try to re-acquire). The single-code-path contract is
        satisfied because the tick body in ``fire_once_already_locked``
        is the IDENTICAL code path; only the lock-acquire entry differs.
        """
        if name not in self._jobs:
            raise KeyError(name)
        if self._scheduler is None:
            raise RuntimeError(
                "JobRegistry has no scheduler bound — call bind_scheduler() "
                "before fire_now()."
            )

        rec = self._jobs[name]
        lock = rec.lock

        try:
            await asyncio.wait_for(
                lock.acquire(),
                timeout=rec.metadata.lock_acquire_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            holder = self._lock_holders.get(name)
            if holder is None:
                raise AlreadyRunning(
                    job_name=name,
                    job_run_id=None,
                    lock_holder_state="unknown",
                    lock_acquired_at=None,
                ) from exc
            raise AlreadyRunning(
                job_name=name,
                job_run_id=holder.job_run_id,
                lock_holder_state=holder.state,
                lock_acquired_at=holder.acquired_at,
            ) from exc

        # Lock acquired and held for the full dispatch. Stage the
        # holder marker so a SECOND fire_now (which would race for
        # this same lock) gets the right 409 shape.
        self._lock_holders[name] = _LockHolder(
            job_run_id=None,  # populated by _open_job_run
            state="starting",
            acquired_at=datetime.now(timezone.utc),
        )
        try:
            assert self._scheduler is not None
            # Direct call into the lock-already-held entry. This is
            # the only place that passes manual_trigger=True; the
            # scheduled path always passes False.
            await self._scheduler.fire_once_already_locked(  # type: ignore[attr-defined]
                rec.job,
                force=True,
                manual_trigger=True,
                triggered_by=triggered_by,
            )
            holder = self._lock_holders.get(name)
            if holder and holder.job_run_id is not None:
                return holder.job_run_id
            # Fallback: read the latest run id for this job.
            async with db_mod.get_session() as session:
                row_id = (
                    await session.execute(
                        select(JobRun.id)
                        .where(JobRun.job_name == name)
                        .order_by(JobRun.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row_id is None:
                    raise RuntimeError(
                        f"fire_now({name!r}) completed but no job_runs row "
                        "was written — single-writer invariant broken?"
                    )
                return row_id
        finally:
            self._lock_holders.pop(name, None)
            lock.release()

    # ------------------------------------------------------------------
    # Helpers exposed to RegisteredScheduler (and the LongRunningJob
    # supervisor that lands in commit #5)
    # ------------------------------------------------------------------

    async def _open_job_run(
        self,
        *,
        job_name: str,
        manual_trigger: bool,
        triggered_by: str,
        started_at: datetime | None = None,
    ) -> int:
        """Insert a ``job_runs`` row in ``status='running'``. Returns ``id``.

        Single-writer point #1 (codex BLOCKER #2). The
        ``idempotency_key`` UNIQUE makes this safe to retry under a
        transient DB blip — a second INSERT with the same key returns
        the existing row's id rather than producing a duplicate.

        Spec §1.7: if this raises, the caller logs + skips the tick;
        cadence_state is not touched. The matrix's "no audit row" row.
        """
        if started_at is None:
            started_at = datetime.now(timezone.utc)
        elif started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)

        key = _idempotency_key(job_name, started_at, triggered_by)

        async with db_mod.get_session() as session:
            row = JobRun(
                job_name=job_name,
                started_at=started_at,
                finished_at=None,
                status="running",
                skip_reason=None,
                error_message=None,
                manual_trigger=1 if manual_trigger else 0,
                triggered_by=triggered_by,
                output_summary=None,
                duration_ms=None,
                idempotency_key=key,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                # Race: the same idempotency_key was written by a
                # retry. Read the existing row id and return that.
                existing_id = (
                    await session.execute(
                        select(JobRun.id).where(
                            JobRun.idempotency_key == key
                        )
                    )
                ).scalar_one_or_none()
                if existing_id is None:
                    raise
                # Update the in-memory holder if we hold the lock for
                # this job (manual or supervisor path).
                self._note_holder(job_name, existing_id)
                return existing_id
            run_id = row.id
            self._note_holder(job_name, run_id)
            return run_id

    def _note_holder(self, job_name: str, run_id: int) -> None:
        """Refresh the in-memory holder marker with the actual run-id.

        Called from :meth:`_open_job_run` once we know the row's id.
        Safe to call when no manual context exists — the scheduled
        path doesn't go through ``fire_now``, so there is no holder
        entry; we set one anyway so a CONCURRENT ``fire_now`` request
        gets the right 409 shape.
        """
        existing = self._lock_holders.get(job_name)
        if existing is None:
            self._lock_holders[job_name] = _LockHolder(
                job_run_id=run_id,
                state="running",
                acquired_at=datetime.now(timezone.utc),
            )
        else:
            existing.job_run_id = run_id
            existing.state = "running"

    async def _close_job_run(
        self,
        run_id: int,
        *,
        status: Literal["ok", "error", "skipped", "cancelled"],
        output_summary: dict[str, Any] | None = None,
        error_message: str | None = None,
        skip_reason: str | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        """Finalize a ``job_runs`` row.

        Single-writer point #2 (codex BLOCKER #2). Ordering invariant
        (§1.7): the RegisteredScheduler override calls this BEFORE
        ``super()._record_tick`` so that if ``_record_tick`` fails the
        audit row already reflects the outcome.
        """
        if finished_at is None:
            finished_at = datetime.now(timezone.utc)
        elif finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=timezone.utc)

        summary_text = (
            json.dumps(output_summary, sort_keys=True, default=str)
            if output_summary is not None
            else None
        )

        async with db_mod.get_session() as session:
            row = (
                await session.execute(
                    select(JobRun).where(JobRun.id == run_id)
                )
            ).scalar_one_or_none()
            if row is None:
                self._log.warning(
                    "jobs.close.row_missing", run_id=run_id, status=status
                )
                return
            row.finished_at = finished_at
            row.status = status
            row.error_message = error_message
            row.skip_reason = skip_reason
            row.output_summary = summary_text
            if row.started_at is not None:
                started = _ensure_utc(row.started_at)
                row.duration_ms = int(
                    (finished_at - started).total_seconds() * 1000
                )
            await session.commit()

    # ------------------------------------------------------------------
    # LongRunningJob supervisor (Spec A commit #5)
    # ------------------------------------------------------------------
    #
    # One supervisor task per registered LongRunningJob. The supervisor
    # owns:
    #   * the (connect, disconnect) cycle's ``job_runs`` row (opened
    #     before ``run()``; closed after it returns/raises),
    #   * the exponential-backoff restart state (reset on clean exit /
    #     operator_stop),
    #   * the restart decision (driven by ``job.exit_intent``).
    #
    # Single-writer invariant (codex BLOCKER #2) is preserved: the
    # supervisor lives IN the registry module and calls the same
    # ``_open_job_run`` / ``_close_job_run`` helpers as the scheduled
    # path. Test ``test_job_runs_single_writer`` covers both.
    #
    # Cancellation race (codex review focus #3): if
    # ``cancel_long_running`` fires while the supervisor is between
    # ``_open_job_run`` and ``await job.run()``, the supervisor's
    # ``finally`` block STILL closes the audit row with
    # ``status='cancelled'`` so no orphan ``running`` row is left.

    async def start_supervisors(self) -> None:
        """Spin up supervisors for any registered :class:`LongRunningJob`.

        Idempotent: a second call is a no-op (the existing supervisor
        tasks keep running). Each registered :class:`LongRunningJob`
        gets exactly one supervisor task; :class:`CadenceLoop`
        instances are driven by :class:`RegisteredScheduler` and are
        skipped here.
        """
        # Lazily construct the stop event so we bind to the CURRENT
        # event loop (important for the test pattern that creates
        # multiple JobRegistry instances across asyncio.run() calls).
        if self._stop_supervisors is None:
            self._stop_supervisors = asyncio.Event()

        for name, rec in self._jobs.items():
            if not isinstance(rec.job, LongRunningJob):
                continue
            if name in self._supervisor_tasks and not self._supervisor_tasks[name].done():
                # Idempotency: existing live task — skip.
                continue
            self._supervisor_tasks[name] = asyncio.create_task(
                self._supervisor(rec.job),
                name=f"supervisor:{name}",
            )
            self._log.info("supervisor.started", job=name)

    async def stop_supervisors(self) -> None:
        """Signal all supervisor tasks to exit + await their completion.

        Sets the shutdown event so each supervisor exits its restart
        loop after the in-flight cycle closes. Tasks that don't return
        within :attr:`_stop_supervisors_timeout_s` are force-cancelled.
        Idempotent (no-op if no supervisors are running).
        """
        if self._stop_supervisors is None:
            return
        self._stop_supervisors.set()

        if not self._supervisor_tasks:
            return

        # Phase 1 — try a clean wait. Each supervisor is expected to
        # observe the stop event between cycles + return cleanly.
        tasks = list(self._supervisor_tasks.values())
        done, pending = await asyncio.wait(
            tasks,
            timeout=self._stop_supervisors_timeout_s,
            return_when=asyncio.ALL_COMPLETED,
        )

        # Phase 2 — force-cancel any stragglers (a supervisor stuck in
        # ``await job.run()`` won't exit until the run-task itself
        # cancels; the supervisor's own try/except handles CancelledError).
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        self._supervisor_tasks.clear()
        # Reset for a possible later restart (tests reuse the registry).
        self._stop_supervisors = None

    async def cancel_long_running(self, name: str) -> None:
        """Operator-initiated stop for a registered :class:`LongRunningJob`.

        Sets the job's ``_exit_intent`` to ``"operator_stop"`` so the
        supervisor's restart-decision branch records the cycle as
        cancelled and does NOT restart. Then calls the job's
        :meth:`LongRunningJob.cancel` hook (subclass-defined cleanup)
        + cancels the supervisor's run-task so ``run()`` exits promptly.

        Idempotent: cancelling an already-stopped job is a no-op.
        """
        if name not in self._jobs:
            raise KeyError(name)
        rec = self._jobs[name]
        if not isinstance(rec.job, LongRunningJob):
            raise TypeError(
                f"cancel_long_running: job {name!r} is a CadenceLoop, not a "
                "LongRunningJob. Use the scheduler's stop path instead."
            )

        # 1. Stamp the exit_intent BEFORE cancelling so the supervisor
        #    sees ``operator_stop`` when run() unwinds (codex review
        #    focus #1 — operator_stop must be distinguishable from
        #    crashed/clean).
        rec.job._exit_intent = "operator_stop"

        # 2. Let the job clean up its own resources (e.g. close a ws).
        try:
            await rec.job.cancel()
        except Exception:  # pragma: no cover - best-effort cleanup
            self._log.exception("supervisor.cancel_hook_failed", job=name)

        # 3. Cancel the supervisor task so ``run()`` exits promptly.
        #    The supervisor's finally + outer try-except handle the
        #    CancelledError + close the audit row with status='cancelled'.
        task = self._supervisor_tasks.get(name)
        if task is not None and not task.done():
            task.cancel()
            # Await the task so the audit row is closed by the time
            # this method returns (matters for the route handler that
            # wants to surface a synchronous "stopped" response).
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                # Supervisor's internal handler already logged + closed
                # the row; swallow here so cancel_long_running returns
                # cleanly to the caller (operator UI).
                pass
            self._supervisor_tasks.pop(name, None)

    async def _lookup_run_id_by_key(
        self, job_name: str, triggered_by: str
    ) -> int | None:
        """Best-effort lookup of a supervisor cycle's run_id by
        ``triggered_by``.

        Used by :meth:`_supervisor` to recover the audit-row id when
        cancellation interrupts ``_open_job_run`` between commit and
        return. The ``triggered_by`` label has the shape
        ``"supervisor#{epoch}.{cycle}"`` — globally unique across
        supervisor task lifetimes (codex round-3 BLOCKER #2 fix). The
        supervisor passes the exact label it used at open time so the
        lookup cannot return an older historical row from a previous
        supervisor instance.

        Returns ``None`` if no row was committed (i.e. cancel landed
        before the DB write).
        """
        try:
            async with db_mod.get_session() as session:
                row_id = (
                    await session.execute(
                        select(JobRun.id)
                        .where(
                            (JobRun.job_name == job_name)
                            & (JobRun.triggered_by == triggered_by)
                        )
                        .order_by(JobRun.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                return row_id
        except Exception:  # pragma: no cover - defensive
            return None

    async def _drain_open_task(
        self, open_task: asyncio.Task[int], job_name: str, label: str
    ) -> int | None:
        """Drain an in-flight ``_open_job_run`` task to a deterministic
        terminal state.

        Used by the supervisor when cancellation lands during the open.
        Cancels the task + awaits its completion so we KNOW whether
        the open committed (returned a run_id) or not (raised). This
        eliminates the codex round-3 BLOCKER #1 race where the
        fallback lookup could miss a row that committed AFTER our
        lookup but BEFORE the open_task fully terminated.

        Returns the run_id on success, ``None`` if the open never
        committed (cancelled-pre-commit or raised). Best-effort lookup
        via :meth:`_lookup_run_id_by_key` covers the residual case
        where the task got cancelled AFTER commit but BEFORE its return.
        """
        # First: cancel the task so it stops doing further DB work.
        open_task.cancel()
        # Then await its completion so we get a deterministic outcome.
        try:
            return await open_task
        except asyncio.CancelledError:
            # Task was cancelled; the commit may or may not have
            # landed. Best-effort recovery via the deterministic
            # idempotency_key. ``label`` includes the epoch so the
            # lookup cannot collide with rows from previous supervisor
            # instances.
            #
            # Codex round-4 IMPORTANT: shield the lookup so a third
            # successive cancel can't propagate out of this method
            # without giving the lookup a chance to complete. If the
            # lookup itself raises CancelledError despite the shield
            # (caller already cancelled mid-await), return None and
            # log — the orphan-reaped endpoint per §1.7 retention is
            # the accepted v1 fallback.
            try:
                return await asyncio.shield(
                    self._lookup_run_id_by_key(job_name, label)
                )
            except asyncio.CancelledError:
                self._log.warning(
                    "supervisor.recovery_lookup_cancelled",
                    job=job_name,
                    label=label,
                )
                return None
        except Exception as exc:
            self._log.warning(
                "supervisor.open_task_drain_failed",
                job=job_name,
                label=label,
                error=str(exc),
            )
            return None

    async def _supervisor(self, job: LongRunningJob) -> None:
        """One coroutine per LongRunningJob. Restart loop.

        Lifecycle of one iteration:

        1. ``_open_job_run`` opens an audit row in ``status='running'``.
        2. ``await job.run()`` (the long-lived body).
        3. ``_close_job_run`` finalizes with one of ``ok | error | cancelled``.
        4. Decide whether to restart based on ``exit_intent``:
           * ``operator_stop`` → no restart, exit the supervisor.
           * ``clean``         → no auto-restart in v1 (spec §3 IMPORTANT #3);
                                 exit the supervisor.
           * ``crashed``       → exp-backoff then restart.
           * ``unset``         → disambiguated: normal return coerces to
                                 ``clean`` (no restart); raised exception
                                 coerces to ``crashed`` (restart). See the
                                 :attr:`LongRunningJob.exit_intent` contract.

        Backoff sequence: 1s → 2s → 4s → 8s → 16s → 32s → 60s → 60s ...
        (capped at :attr:`_backoff_cap_s`). The cap prevents runaway
        memory/CPU from a job that crashes immediately on every restart
        (codex review focus #2 — the cap is the runaway-safety net).
        """
        assert self._stop_supervisors is not None  # set by start_supervisors

        backoff_s = self._backoff_initial_s
        # Per-supervisor cycle counter — included in ``triggered_by`` so
        # each cycle's idempotency_key is unique even when two cycles
        # land inside the same wall-clock second (test setups with a
        # fake clock; high-frequency restart loops). Production cycles
        # are separated by ≥1s of backoff so they naturally differ —
        # the counter is a belt-and-suspenders correctness fix, not a
        # performance optimization.
        # Per-registry epoch — included in ``triggered_by`` so the
        # cycle-recovery lookup cannot select a row from a PRIOR
        # supervisor instance (codex round-3 BLOCKER #2 fix).
        self._supervisor_epoch += 1
        epoch = self._supervisor_epoch
        cycle = 0
        while not self._stop_supervisors.is_set():
            cycle += 1
            # Reset exit_intent for THIS cycle so a previous cycle's
            # state doesn't leak (e.g. a fresh restart after a crash
            # should not still read 'crashed' before run() starts).
            job._exit_intent = "unset"

            # Codex BLOCKER fix: single try/finally enclosing BOTH
            # _open_job_run and job.run() — guarantees the audit row
            # is closed even if CancelledError lands during _open_job_run.
            # ``run_id`` stays None until _open_job_run returns; the
            # finally block keys off ``run_id is not None`` so we don't
            # try to close a row that was never opened.
            run_id: int | None = None
            close_status: Literal["ok", "error", "cancelled"] = "ok"
            close_error: str | None = None
            close_summary: dict[str, Any] | None = None
            restart_after: bool = False
            should_raise = False

            triggered_by = f"supervisor#{epoch}.{cycle}"
            try:
                # Codex round-2 + round-3 BLOCKER fix: protect the open
                # from a cancel-between-commit-and-assignment race. We
                # launch ``_open_job_run`` as a Task and await it under
                # a shield so external cancellation cannot raise
                # CancelledError into THIS frame before ``run_id`` is
                # bound. The shield is bounded to the open only —
                # ``job.run()`` below is NOT shielded so operator stop
                # still cancels the body promptly.
                #
                # If the outer task is cancelled while we're in the
                # shielded await, ``_drain_open_task`` cancels the
                # in-flight open + awaits its terminal state, ensuring
                # the open cannot commit AFTER our fallback decision
                # (round-3 BLOCKER #1 fix).
                open_task = asyncio.create_task(
                    self._open_job_run(
                        job_name=job.name,
                        manual_trigger=False,
                        triggered_by=triggered_by,
                    )
                )
                try:
                    try:
                        run_id = await asyncio.shield(open_task)
                    except asyncio.CancelledError:
                        # Outer task was cancelled while awaiting the
                        # shielded open. Drain the open task to a
                        # deterministic terminal state (committed or
                        # rolled back) so the finally close path runs
                        # on the right row id.
                        should_raise = True
                        run_id = await self._drain_open_task(
                            open_task, job.name, triggered_by
                        )
                        raise
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Audit-row open failed (DB blip). Log + backoff +
                    # retry. No audit row exists, so no close to do.
                    self._log.exception(
                        "supervisor.open_job_run_failed", job=job.name
                    )
                    await self._sleep_with_stop(backoff_s)
                    backoff_s = min(backoff_s * 2, self._backoff_cap_s)
                    continue

                # Audit row OPEN. From here, the outer finally guarantees
                # the row gets closed.
                try:
                    await job.run()
                    # run() returned. The job is expected to have set
                    # _exit_intent before returning; if it didn't, default
                    # to 'clean' — returning without an exception is a
                    # CLEAN exit by definition. (Spec §3 IMPORTANT #3:
                    # 'unset' is a defensive default that the supervisor
                    # MUST disambiguate; on normal return we coerce to
                    # 'clean' to match the contract documented in
                    # :attr:`LongRunningJob.exit_intent`.)
                    intent = job.exit_intent
                    if intent == "unset":
                        intent = "clean"
                        job._exit_intent = "clean"
                    if intent == "operator_stop":
                        close_status = "cancelled"
                        close_summary = {"notes": "operator stop"}
                        restart_after = False
                    else:  # 'clean'
                        close_status = "ok"
                        close_summary = {"notes": "clean exit"}
                        restart_after = False
                    backoff_s = self._backoff_initial_s
                except asyncio.CancelledError:
                    # Supervisor task was cancelled (typically via
                    # cancel_long_running). The job's _exit_intent was
                    # already stamped to 'operator_stop' by
                    # cancel_long_running BEFORE the task.cancel() call.
                    close_status = "cancelled"
                    close_summary = {"notes": "cancelled by supervisor"}
                    restart_after = False
                    should_raise = True
                    raise
                except Exception as exc:
                    # Unexpected crash → restart with backoff.
                    job._exit_intent = "crashed"
                    close_status = "error"
                    close_error = str(exc)
                    restart_after = True
                    self._log.exception(
                        "supervisor.run_crashed", job=job.name
                    )
            finally:
                # Single close point — guaranteed reached even on
                # CancelledError during _open_job_run or job.run().
                if run_id is not None:
                    # Cancellation during the OPEN path leaves
                    # close_status='ok' (its initial value). Override
                    # so the row reflects what actually happened.
                    if should_raise and close_status == "ok":
                        close_status = "cancelled"
                        close_summary = {
                            "notes": "cancelled during open"
                        }
                    await self._safe_close(
                        run_id,
                        status=close_status,
                        output_summary=close_summary,
                        error_message=close_error,
                    )

            if should_raise:
                # CancelledError path — clean up + re-raise.
                self._supervisor_tasks.pop(job.name, None)
                raise asyncio.CancelledError()

            if not restart_after:
                # Clean exit or operator_stop — supervisor done.
                self._supervisor_tasks.pop(job.name, None)
                return

            # Crashed branch — sleep with backoff, then loop.
            if self._stop_supervisors.is_set():
                self._supervisor_tasks.pop(job.name, None)
                return
            await self._sleep_with_stop(backoff_s)
            backoff_s = min(backoff_s * 2, self._backoff_cap_s)

        # Stop event fired between cycles — exit cleanly.
        self._supervisor_tasks.pop(job.name, None)

    async def _safe_close(
        self,
        run_id: int,
        *,
        status: Literal["ok", "error", "cancelled"],
        output_summary: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        """Wrap :meth:`_close_job_run` so a close-write failure does not
        propagate out of the supervisor loop.
        """
        try:
            await self._close_job_run(
                run_id,
                status=status,
                output_summary=output_summary,
                error_message=error_message,
            )
        except Exception:  # pragma: no cover - DB unavailable path
            self._log.exception(
                "supervisor.close_job_run_failed",
                run_id=run_id,
                intended_status=status,
            )

    async def _sleep_with_stop(self, delay_s: float) -> None:
        """Sleep ``delay_s`` seconds OR until the stop event fires —
        whichever comes first.

        The supervisor uses this for exp-backoff so :meth:`stop_supervisors`
        can interrupt a long sleep without waiting for the full cap.
        Tests override :attr:`_sleep` (with a no-op) to skip wall-time.
        """
        assert self._stop_supervisors is not None
        if delay_s <= 0:
            return
        stop_wait = asyncio.create_task(self._stop_supervisors.wait())
        # Use the (test-overridable) sleep hook here so a fake clock
        # can short-circuit the delay.
        sleep_wait = asyncio.create_task(self._sleep(delay_s))
        try:
            done, pending = await asyncio.wait(
                {stop_wait, sleep_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            stop_wait.cancel()
            sleep_wait.cancel()
            raise


# ---------------------------------------------------------------------------
# Cadence helper
# ---------------------------------------------------------------------------


def _cadence_seconds(job: "CadenceLoop | LongRunningJob") -> float | None:
    """Resolve a CadenceLoop's nominal cadence in seconds.

    For interval-driven loops, that's ``schedule.interval_seconds``.
    For cron-driven loops, the next-due delta from "now" approximates
    the cadence (e.g. daily cron → ~86400). LongRunningJob doesn't
    expose ``.schedule``; ``cadence_seconds=None`` means health
    falls back to ``"green"`` on ``ok`` per the §1.6 table.
    """
    schedule = getattr(job, "schedule", None)
    if schedule is None:
        return None
    if getattr(schedule, "interval_seconds", None):
        return float(schedule.interval_seconds)
    # Approximate cron cadence as the next-due distance from now.
    try:
        now = datetime.now(timezone.utc)
        nxt = schedule.next_due_after(now)
        delta = (nxt - now).total_seconds()
        if delta <= 0:
            return None
        return float(delta)
    except Exception:  # pragma: no cover - defensive
        return None


__all__ = [
    "AlreadyRunning",
    "JobMetadata",
    "JobRegistry",
    "JobView",
    "SourceKind",
    "_derive_health",
    "_idempotency_key",
]
