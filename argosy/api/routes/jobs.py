"""Sprint A commit #4 — ``/api/jobs`` routes + admin auth gate.

Spec: ``docs/superpowers/specs/2026-05-29-jobs-registry-design.md``
- Commit #4 detail (around line 536)
- §1.4 — 409 contention response shape
- §8 — API appendix (full route shapes)

Route inventory
===============

Open (no auth):

* ``GET /api/jobs`` — list every registered job with its ``JobView`` snapshot.
* ``GET /api/jobs/{name}`` — single ``JobView`` + recent runs.
* ``GET /api/jobs/{name}/runs?limit=N&include_skipped=false`` — paginated
  ``job_runs`` history. ``include_skipped`` defaults to False (the spec
  IMPORTANT #4 calls out that skipped rows must be surfacable on demand
  but excluded by default so the operator sees real ticks).

Admin-only (``X-Argosy-Admin`` header required):

* ``POST /api/jobs/{name}/run-now`` — manual trigger. 202 + ``{job_run_id}``;
  409 with the §1.4 shape if the per-job lock is held.
* ``POST /api/jobs/{name}/stop`` — operator stop for ``LongRunningJob``.
  400 if the target is a ``CadenceLoop``.
* ``POST /api/jobs/{name}/reconnect`` — stop+restart for ``LongRunningJob``.

Admin-auth posture (codex BLOCKER #1)
=====================================

The mutating routes are gated by :func:`argosy.api.auth.require_admin_token`.
The dependency reads the ``X-Argosy-Admin`` header and constant-time-
compares it against ``settings.admin_token`` (loaded from
``ARGOSY_ADMIN_TOKEN``).

If the env var is UNSET at app-startup time, the mutating routes
refuse to mount — only the read-only routes are registered. The
refusal is at MOUNT time (not request time) so a stray browser tab
can't trigger a 401 storm and a CORS-permitted UI can't accidentally
fire LLM-cost work even with a misconfigured FastAPI middleware
chain.

The boundary between open and gated routes is explicit and verified
by tests in ``tests/test_api_jobs.py``: only the three POST routes
take ``Depends(require_admin_token)``; the four GET routes are open.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from argosy.api.auth import SpecError, require_admin_token
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import LongRunningJob
from argosy.services.jobs.registry import (
    AlreadyRunning,
    JobMetadata,
    JobRegistry,
    JobView,
)
from argosy.state import db as db_mod
from argosy.state.models import JobRun
from sqlalchemy import select


_log = get_logger("argosy.api.routes.jobs")


# ---------------------------------------------------------------------------
# Pydantic DTOs — kept here so OpenAPI's /docs reflects the actual shape
# (codex review focus: response shapes should be typed so the schema
# document is accurate).
# ---------------------------------------------------------------------------


class JobMetadataDTO(BaseModel):
    """Static metadata for a registered job. Mirrors :class:`JobMetadata`."""

    name: str
    schedule_cron: str | None
    schedule_human: str
    source_kind: str
    description: str
    long_running: bool
    lock_acquire_timeout_s: float

    @classmethod
    def from_metadata(cls, m: JobMetadata) -> "JobMetadataDTO":
        return cls(
            name=m.name,
            schedule_cron=m.schedule_cron,
            schedule_human=m.schedule_human,
            source_kind=m.source_kind,
            description=m.description,
            long_running=m.long_running,
            lock_acquire_timeout_s=m.lock_acquire_timeout_s,
        )


class JobViewDTO(BaseModel):
    """Computed view shape returned by GET /api/jobs and /api/jobs/{name}."""

    name: str
    schedule_cron: str | None
    schedule_human: str
    source_kind: str
    description: str
    long_running: bool
    last_run_at: datetime | None
    last_run_status: str | None
    last_run_error: str | None
    next_run_at: datetime | None
    currently_running_run_id: int | None
    health: str

    @classmethod
    def from_view(cls, v: JobView) -> "JobViewDTO":
        return cls(
            name=v.metadata.name,
            schedule_cron=v.metadata.schedule_cron,
            schedule_human=v.metadata.schedule_human,
            source_kind=v.metadata.source_kind,
            description=v.metadata.description,
            long_running=v.metadata.long_running,
            last_run_at=v.last_run_at,
            last_run_status=v.last_run_status,
            last_run_error=v.last_run_error,
            next_run_at=v.next_run_at,
            currently_running_run_id=v.currently_running_run_id,
            health=v.health,
        )


class JobsListResponse(BaseModel):
    """``GET /api/jobs`` body."""

    scheduler_running: bool
    jobs: list[JobViewDTO]


class JobRunDTO(BaseModel):
    """One ``job_runs`` row in the format the UI / history endpoints want."""

    id: int
    job_name: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    skip_reason: str | None
    error_message: str | None
    manual_trigger: bool
    triggered_by: str | None
    output_summary: str | None
    duration_ms: int | None


class JobDetailResponse(BaseModel):
    """``GET /api/jobs/{name}`` body."""

    metadata: JobMetadataDTO
    view: JobViewDTO
    recent_runs: list[JobRunDTO]


class JobRunsResponse(BaseModel):
    """``GET /api/jobs/{name}/runs`` body."""

    runs: list[JobRunDTO]
    has_more: bool
    next_before_id: int | None


class RunNowResponse(BaseModel):
    """``POST /api/jobs/{name}/run-now`` 202 body."""

    job_run_id: int
    name: str
    started_at: datetime


class StopResponse(BaseModel):
    """``POST /api/jobs/{name}/stop`` 202 body."""

    name: str
    stopped_at: datetime


class ReconnectResponse(BaseModel):
    """``POST /api/jobs/{name}/reconnect`` 202 body.

    ``new_job_run_id`` is ``None`` when the freshly-spun supervisor
    hasn't opened its first job_runs row yet (race window — the UI
    polls ``/api/jobs/{name}`` shortly after to pick up the id). Spec
    §8 documents the field as ``new_job_run_id`` without an explicit
    null marker, but representing the not-yet-known case as ``null``
    is preferable to a sentinel int (codex round-1 IMPORTANT).
    """

    name: str
    new_job_run_id: int | None


# ---------------------------------------------------------------------------
# Router factory — open-only routes always mount; mutating routes mount
# ONLY when ARGOSY_ADMIN_TOKEN is set (Spec §Commit #4 — refuse-to-mount).
# ---------------------------------------------------------------------------


def _require_registry(request: Request) -> JobRegistry:
    """Resolve the JobRegistry from app.state. 503 if the registry
    failed to construct at startup (should not happen — startup builds
    it unconditionally per commit #3b).
    """
    registry = getattr(request.app.state, "job_registry", None)
    if registry is None:
        raise SpecError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            body={"error": "job_registry_not_initialized"},
        )
    return registry


def _scheduler_running(request: Request) -> bool:
    """Whether the scheduler is currently driving ticks. False when
    ``ARGOSY_RUN_SCHEDULER=0`` (commit #3b) or when shutdown has set
    the stop event.
    """
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return False
    stop_event = getattr(scheduler, "_stop", None)
    if stop_event is not None and stop_event.is_set():
        return False
    return True


async def _recent_runs(
    job_name: str,
    *,
    limit: int,
    include_skipped: bool,
    before_id: int | None = None,
) -> tuple[list[JobRunDTO], bool, int | None]:
    """Fetch most-recent ``job_runs`` rows for ``job_name``.

    Returns ``(rows, has_more, next_before_id)``. ``rows`` is newest-
    first; ``has_more`` is True if more rows exist beyond the page;
    ``next_before_id`` is the cursor for the next page (the id of the
    oldest row in the current page).
    """
    async with db_mod.get_session() as session:
        stmt = select(JobRun).where(JobRun.job_name == job_name)
        if not include_skipped:
            stmt = stmt.where(JobRun.status != "skipped")
        if before_id is not None:
            stmt = stmt.where(JobRun.id < before_id)
        # Over-fetch by one row so we can compute has_more without a
        # second COUNT query.
        stmt = stmt.order_by(JobRun.id.desc()).limit(limit + 1)
        result = (await session.execute(stmt)).scalars().all()
    has_more = len(result) > limit
    rows = result[:limit]
    next_before = rows[-1].id if has_more and rows else None
    return (
        [
            JobRunDTO(
                id=r.id,
                job_name=r.job_name,
                started_at=r.started_at,
                finished_at=r.finished_at,
                status=r.status,
                skip_reason=r.skip_reason,
                error_message=r.error_message,
                manual_trigger=bool(r.manual_trigger),
                triggered_by=r.triggered_by,
                output_summary=r.output_summary,
                duration_ms=r.duration_ms,
            )
            for r in rows
        ],
        has_more,
        next_before,
    )


# ---------------------------------------------------------------------------
# Open router — always mounted
# ---------------------------------------------------------------------------


def _build_open_router() -> APIRouter:
    """The read-only routes — listed separately from mutating routes so
    the boundary between "open" and "gated" surface area is enforced
    structurally (cannot accidentally add an open mutating route).
    """
    router = APIRouter(prefix="/api/jobs", tags=["jobs"])

    @router.get("", response_model=JobsListResponse)
    async def list_jobs(request: Request) -> JobsListResponse:
        registry = _require_registry(request)
        views = await registry.list()
        return JobsListResponse(
            scheduler_running=_scheduler_running(request),
            jobs=[JobViewDTO.from_view(v) for v in views],
        )

    @router.get("/{name}", response_model=JobDetailResponse)
    async def get_job(name: str, request: Request) -> JobDetailResponse:
        registry = _require_registry(request)
        try:
            view = await registry.get(name)
        except KeyError:
            raise SpecError(
                status_code=status.HTTP_404_NOT_FOUND,
                body={"error": "job_not_found", "name": name},
            )
        metadata = registry.get_metadata(name)
        # Default: 20 most-recent rows, skipped excluded.
        runs, _has_more, _next = await _recent_runs(
            name, limit=20, include_skipped=False
        )
        return JobDetailResponse(
            metadata=JobMetadataDTO.from_metadata(metadata),
            view=JobViewDTO.from_view(view),
            recent_runs=runs,
        )

    @router.get("/{name}/runs", response_model=JobRunsResponse)
    async def get_runs(
        name: str,
        request: Request,
        limit: int = Query(default=20, ge=1, le=200),
        include_skipped: bool = Query(default=False),
        before_id: int | None = Query(default=None, ge=1),
    ) -> JobRunsResponse:
        registry = _require_registry(request)
        if name not in registry.names():
            raise SpecError(
                status_code=status.HTTP_404_NOT_FOUND,
                body={"error": "job_not_found", "name": name},
            )
        rows, has_more, next_before = await _recent_runs(
            name,
            limit=limit,
            include_skipped=include_skipped,
            before_id=before_id,
        )
        return JobRunsResponse(
            runs=rows, has_more=has_more, next_before_id=next_before
        )

    return router


# ---------------------------------------------------------------------------
# Mutating router — mounted ONLY when ARGOSY_ADMIN_TOKEN is set
# ---------------------------------------------------------------------------


def _build_admin_router() -> APIRouter:
    """The mutating routes. Every endpoint takes
    ``Depends(require_admin_token)`` so the gate is structurally
    impossible to bypass (no shared helpers that skip auth; no
    OPTIONS-method auto-handler that would bleed through CORS).
    """
    router = APIRouter(
        prefix="/api/jobs",
        tags=["jobs"],
        dependencies=[Depends(require_admin_token)],
    )

    @router.post(
        "/{name}/run-now",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=RunNowResponse,
    )
    async def run_now(name: str, request: Request) -> RunNowResponse:
        registry = _require_registry(request)
        if name not in registry.names():
            raise SpecError(
                status_code=status.HTTP_404_NOT_FOUND,
                body={"error": "job_not_found", "name": name},
            )
        if not _scheduler_running(request):
            # Spec §8: 503 when scheduler is off — manual trigger needs
            # the scheduler bound (fire_now dispatches through it).
            raise SpecError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                body={"error": "scheduler_not_running"},
            )
        started_at = datetime.now(timezone.utc)
        try:
            run_id = await registry.fire_now(name, triggered_by="user:admin")
        except AlreadyRunning as exc:
            # Spec §1.4 / §8: 409 with the full lock_holder_state shape.
            raise SpecError(
                status_code=status.HTTP_409_CONFLICT,
                body=exc.to_response_body(),
            )
        return RunNowResponse(job_run_id=run_id, name=name, started_at=started_at)

    @router.post(
        "/{name}/stop",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=StopResponse,
    )
    async def stop_job(name: str, request: Request) -> StopResponse:
        registry = _require_registry(request)
        if name not in registry.names():
            raise SpecError(
                status_code=status.HTTP_404_NOT_FOUND,
                body={"error": "job_not_found", "name": name},
            )
        job = registry.get_job(name)
        if not isinstance(job, LongRunningJob):
            # Spec §8: 400 for non-long-running.
            raise SpecError(
                status_code=status.HTTP_400_BAD_REQUEST,
                body={"error": "not_long_running", "name": name},
            )
        await registry.cancel_long_running(name)
        return StopResponse(name=name, stopped_at=datetime.now(timezone.utc))

    @router.post(
        "/{name}/reconnect",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ReconnectResponse,
    )
    async def reconnect_job(name: str, request: Request) -> ReconnectResponse:
        registry = _require_registry(request)
        if name not in registry.names():
            raise SpecError(
                status_code=status.HTTP_404_NOT_FOUND,
                body={"error": "job_not_found", "name": name},
            )
        job = registry.get_job(name)
        if not isinstance(job, LongRunningJob):
            raise SpecError(
                status_code=status.HTTP_400_BAD_REQUEST,
                body={"error": "not_long_running", "name": name},
            )
        # Codex round-2 BLOCKER fix: snapshot the max ``job_runs.id``
        # for this job BEFORE the stop+restart so we can distinguish
        # the freshly-opened cycle's row from the (just-closed)
        # previous cycle's row. Without this snapshot, the post-
        # restart "latest id" query could return the OLD row (the one
        # closed with status='cancelled') if the new supervisor hasn't
        # committed its first INSERT yet — that would mis-attribute a
        # stale id as the "new" run.
        async with db_mod.get_session() as session:
            prev_max_id = (
                await session.execute(
                    select(JobRun.id)
                    .where(JobRun.job_name == name)
                    .order_by(JobRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

        # Stop first; cancel_long_running awaits the supervisor task so
        # by the time it returns the row is closed + the supervisor is
        # gone. Then re-spin a fresh supervisor via start_supervisors
        # (idempotent — only the absent ones get spun up).
        await registry.cancel_long_running(name)
        # Clear the exit_intent so the fresh supervisor doesn't immediately
        # interpret a leftover ``operator_stop`` as "do not restart".
        job._exit_intent = "unset"
        await registry.start_supervisors()

        # Look up the freshly-opened job_run id (codex round-3 BLOCKER
        # fix): the watermark check (`row_id > prev_max_id`) alone is
        # not enough — a concurrent ``run-now`` could insert a row that
        # also passes the watermark but is NOT this reconnect cycle's.
        # Additionally filter by ``triggered_by LIKE 'supervisor#%'`` so
        # we only accept rows opened by the supervisor restart path
        # (which uses ``triggered_by=f"supervisor#{epoch}.{cycle}"``).
        # Combined, the filter is "supervisor-opened AND post-watermark"
        # which uniquely identifies the new reconnect cycle.
        new_id: int | None = None
        async with db_mod.get_session() as session:
            stmt = (
                select(JobRun.id)
                .where(JobRun.job_name == name)
                .where(JobRun.triggered_by.like("supervisor#%"))
                .order_by(JobRun.id.desc())
                .limit(1)
            )
            row_id = (await session.execute(stmt)).scalar_one_or_none()
            if row_id is not None and (
                prev_max_id is None or row_id > prev_max_id
            ):
                new_id = row_id
        # If the new supervisor cycle hasn't committed its first
        # job_runs row yet, surface ``null`` (Pydantic accepts None
        # because the field is typed ``int | None``). UI polls
        # /api/jobs/{name} for the eventual id (codex round-1 IMPORTANT
        # — sentinels like -1 invite misinterpretation).
        return ReconnectResponse(name=name, new_job_run_id=new_id)

    return router


# ---------------------------------------------------------------------------
# Public mounter — called from argosy/api/main.py
# ---------------------------------------------------------------------------


def register_routers(app: FastAPI) -> None:
    """Mount the /api/jobs routers on ``app``.

    The open router (``GET`` endpoints) ALWAYS mounts so monitoring
    tools can probe job state without any auth setup.

    The mutating router (``POST`` endpoints) mounts ONLY when
    ``ARGOSY_ADMIN_TOKEN`` env var is set (loaded into
    ``settings.admin_token`` by ``argosy.config.get_settings``). When
    unset, we log a startup WARNING and skip the mount — Spec A
    commit #4 BLOCKER #1: an unset token MUST result in the routes
    being absent, not just a 401 on every request.

    Refusal is at MOUNT TIME so the 404 from FastAPI is the operator's
    signal that the gate isn't configured (rather than a 401 implying
    "wrong token; try again", which would invite brute-forcing).
    """
    from argosy.config import get_settings

    # Register the SpecError handler so error bodies match §8 / §1.4
    # exactly (top-level JSON object, no ``detail`` envelope).
    # Idempotent — multiple create_app() calls in the same process are
    # safe because we only overwrite the existing handler for this
    # specific exception class.
    @app.exception_handler(SpecError)
    async def _spec_error_handler(_request: Request, exc: SpecError):  # noqa: ARG001
        return JSONResponse(status_code=exc.status_code, content=exc.body)

    app.include_router(_build_open_router())

    settings = get_settings()
    if settings.admin_token:
        app.include_router(_build_admin_router())
        _log.info("jobs.admin_routes_mounted")
    else:
        _log.warning(
            "jobs.admin_routes_refused",
            reason="ARGOSY_ADMIN_TOKEN env var not set",
            note=(
                "POST /api/jobs/*/run-now, /stop, /reconnect routes "
                "will return 404. Set ARGOSY_ADMIN_TOKEN and restart "
                "the server to enable them."
            ),
        )


__all__ = [
    "register_routers",
    "JobMetadataDTO",
    "JobViewDTO",
    "JobsListResponse",
    "JobDetailResponse",
    "JobRunDTO",
    "JobRunsResponse",
    "RunNowResponse",
    "StopResponse",
    "ReconnectResponse",
]
