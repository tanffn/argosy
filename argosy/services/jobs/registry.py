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
from argosy.orchestrator.loops.base import CadenceLoop
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
    job: CadenceLoop  # LongRunningJob lands in commit #5
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
        job: CadenceLoop,
        metadata: JobMetadata,
    ) -> None:
        """Register a job + its metadata.

        v1 only accepts :class:`CadenceLoop` instances. The
        ``LongRunningJob`` branch lands in commit #5 — the type union is
        widened then.
        """
        if metadata.name != job.name:
            raise ValueError(
                f"metadata.name ({metadata.name!r}) must match job.name "
                f"({job.name!r})"
            )
        if metadata.name in self._jobs:
            raise ValueError(f"job {metadata.name!r} already registered")
        self._jobs[metadata.name] = _RegisteredJob(metadata=metadata, job=job)
        self._log.info(
            "jobs.registered",
            job=metadata.name,
            source_kind=metadata.source_kind,
            long_running=metadata.long_running,
        )

    def names(self) -> list[str]:
        return list(self._jobs.keys())

    def get_job(self, name: str) -> CadenceLoop:
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
    # Placeholders for commit #5 (LongRunningJob supervisor)
    # ------------------------------------------------------------------

    async def cancel_long_running(self, name: str) -> None:
        """Cancel a LongRunningJob. Placeholder until commit #5."""
        raise NotImplementedError(
            "cancel_long_running lands in Sprint A commit #5."
        )

    async def start_supervisors(self) -> None:
        """Spin up supervisors for any registered LongRunningJob.

        Placeholder. In commit #5 this iterates registered LongRunning
        jobs and ``asyncio.create_task(self._supervise_longrunning(j))``
        for each. The CadenceLoop scheduled-path is already driven by
        :class:`RegisteredScheduler` so there is nothing to do for them
        here.
        """
        # Intentional no-op until commit #5.
        return None

    async def stop_supervisors(self) -> None:
        """Cancel all supervisor tasks. Placeholder until commit #5."""
        # Intentional no-op until commit #5.
        return None


# ---------------------------------------------------------------------------
# Cadence helper
# ---------------------------------------------------------------------------


def _cadence_seconds(job: CadenceLoop) -> float | None:
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
