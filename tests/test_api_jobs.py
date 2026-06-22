"""Sprint A commit #4 — tests for /api/jobs routes + admin auth gate.

Spec: ``docs/superpowers/specs/2026-05-29-jobs-registry-design.md`` §8 + §Commit #4.

Test scenarios (mirrors the prompt + spec §9):

* ``GET /api/jobs`` with no admin token → 200; returns the registry list.
* ``POST /api/jobs/X/run-now`` without header → 401.
* ``POST /api/jobs/X/run-now`` with wrong token → 401.
* ``POST /api/jobs/X/run-now`` with valid token → 202 + ``{job_run_id}``.
* ``POST /api/jobs/X/run-now`` against an already-running job → 409 with
  the documented §1.4 shape (lock_holder_state, conflict_reason, etc).
* ``POST /api/jobs/X/stop`` against a CadenceLoop → 400 ``not_long_running``.
* ``POST /api/jobs/X/reconnect`` against a LongRunningJob → 202.
* When ``ARGOSY_ADMIN_TOKEN`` env is unset at startup → mutating routes
  refuse to mount; GET /api/jobs still works; the POST routes 404.
* ``GET /api/jobs/X/runs?limit=5`` returns at most 5; ``include_skipped``
  default-excludes status='skipped' rows; ``=true`` includes them.

Test command::

    D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest \\
        -m "not llm_eval" tests/test_api_jobs.py -v
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import select
from starlette.testclient import TestClient

from argosy.state.models import Base

from argosy.orchestrator.loops.base import (
    CadenceLoop,
    LongRunningJob,
    LoopSchedule,
)
from argosy.services.jobs import JobMetadata, JobRegistry
from argosy.state import db as db_mod
from argosy.state.models import JobRun


# ---------------------------------------------------------------------------
# Test jobs
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def engine(tmp_path):
    """Override the conftest in-memory engine with a FILE-backed one — for the
    jobs tests ONLY.

    These tests run with the scheduler ON, so the scheduler (run_forever +
    supervisors, on the TestClient portal thread) and the test thread hit the DB
    concurrently. The conftest's shared-single-connection ``:memory:`` engine
    corrupted reads under that cross-thread/cross-loop access — seeded
    ``job_runs`` rows went missing and reconnect hit sqlite errors,
    intermittently (passed alone, failed amid siblings). A file-backed DB gives
    each connection its own handle to ONE consistent on-disk DB; WAL +
    busy_timeout let the concurrent writers coexist. Scoped to this module so the
    global ``:memory:`` fixture (which other async tests rely on, and which a
    file-backed engine would expose to write-lock contention) is unchanged.
    """
    test_url = f"sqlite+aiosqlite:///{tmp_path / 'jobs_test.db'}"
    eng = db_mod.init_engine(test_url)

    @sa.event.listens_for(eng.sync_engine, "connect")
    def _pragmas(dbapi_conn, _rec):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield None
    finally:
        await db_mod.dispose_engine()


class _OkLoop(CadenceLoop):
    name = "api_ok_loop"

    def __init__(self) -> None:
        super().__init__(schedule=LoopSchedule(interval_seconds=60), enabled=True)
        self.last_output_summary = None

    async def tick(self, *, now=None):  # type: ignore[override]
        self.last_output_summary = {"counts": {"ok": 1}}


class _SlowLoop(CadenceLoop):
    """Holds the lock long enough to observe 409 contention."""

    name = "api_slow_loop"

    def __init__(self) -> None:
        super().__init__(schedule=LoopSchedule(interval_seconds=60), enabled=True)
        self.started = asyncio.Event()

    async def tick(self, *, now=None):  # type: ignore[override]
        self.started.set()
        await asyncio.sleep(2.0)


class _StubLongRunning(LongRunningJob):
    """Minimal LongRunningJob for stop/reconnect endpoint tests.

    Its ``run()`` blocks on an event so the supervisor's
    cancel-then-restart cycle is observable. The connection_status flips
    to ``connected`` as soon as ``run()`` begins.
    """

    name = "api_longrunner"

    def __init__(self) -> None:
        self._exit_intent = "unset"
        self._status = "stopped"
        self._stop_event: asyncio.Event | None = None
        self.run_calls = 0

    def connection_status(self) -> str:
        return self._status

    async def run(self) -> None:
        self.run_calls += 1
        self._status = "connected"
        self._stop_event = asyncio.Event()
        try:
            await self._stop_event.wait()
        finally:
            self._status = "stopped"

    async def cancel(self) -> None:
        # When the operator stops the job, release the inner sleep so
        # run() unwinds promptly. The supervisor's task.cancel() ALSO
        # interrupts the wait, but this gives us a clean-shutdown path.
        if self._stop_event is not None:
            self._stop_event.set()


def _meta(name: str, **overrides) -> JobMetadata:
    defaults = dict(
        name=name,
        schedule_cron=None,
        schedule_human="every 60s",
        source_kind="maintenance",
        description=f"test {name}",
        long_running=False,
        lock_acquire_timeout_s=0.2,
    )
    defaults.update(overrides)
    return JobMetadata(**defaults)


# ---------------------------------------------------------------------------
# App-building helper
# ---------------------------------------------------------------------------


def _build_app_with_jobs(
    *,
    admin_token: str | None,
    monkeypatch: pytest.MonkeyPatch,
    register_jobs: list[tuple[CadenceLoop | LongRunningJob, JobMetadata]] | None = None,
):
    """Build a fresh FastAPI app with the requested admin-token env state
    and a controlled registry of jobs.

    We can't rely on ``create_app``'s startup hook to register our test
    jobs (the default loops don't include our stubs). Instead, we build
    the app + push the test jobs into ``app.state.job_registry`` once
    the TestClient context manager has triggered startup.

    Returns (app, TestClient). Caller manages the TestClient context.
    """
    if admin_token is None:
        monkeypatch.delenv("ARGOSY_ADMIN_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ARGOSY_ADMIN_TOKEN", admin_token)
    # Default: scheduler ON (manual fire_now depends on it).
    monkeypatch.delenv("ARGOSY_RUN_SCHEDULER", raising=False)

    from argosy.config import reload_settings

    reload_settings()

    from argosy.api.main import create_app

    app = create_app()
    tc = TestClient(app)
    # Force startup to fire.
    tc.__enter__()
    try:
        # Push the requested jobs through the live registry +
        # scheduler so the routes see them.
        registry: JobRegistry = app.state.job_registry
        scheduler = app.state.scheduler
        if register_jobs:
            for job, meta in register_jobs:
                if isinstance(job, CadenceLoop):
                    scheduler.register_loop(job)
                registry.register(job=job, metadata=meta)
            # Spin supervisors for any LongRunningJob we just added.
            asyncio.get_event_loop().run_until_complete(
                registry.start_supervisors()
            )
        yield app, tc
    finally:
        tc.__exit__(None, None, None)
        # Tear down the settings cache so the next test gets fresh env.
        reload_settings()


@pytest.fixture
def app_with_admin_token(monkeypatch: pytest.MonkeyPatch):
    """Yields ``(app, TestClient)`` with ``ARGOSY_ADMIN_TOKEN=secret123`` and a
    single ``api_ok_loop`` registered.
    """
    gen = _build_app_with_jobs(
        admin_token="secret123",
        monkeypatch=monkeypatch,
        register_jobs=[(_OkLoop(), _meta("api_ok_loop"))],
    )
    yield from gen


@pytest.fixture
def app_without_admin_token(monkeypatch: pytest.MonkeyPatch):
    """Yields ``(app, TestClient)`` with NO admin token configured."""
    gen = _build_app_with_jobs(
        admin_token=None,
        monkeypatch=monkeypatch,
        register_jobs=[(_OkLoop(), _meta("api_ok_loop"))],
    )
    yield from gen


# ---------------------------------------------------------------------------
# GET /api/jobs — open route
# ---------------------------------------------------------------------------


def test_get_jobs_open_no_auth_required(engine: None, app_with_admin_token) -> None:
    app, tc = app_with_admin_token
    # No header — must still return 200.
    response = tc.get("/api/jobs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "scheduler_running" in body
    assert "jobs" in body
    job_names = [j["name"] for j in body["jobs"]]
    assert "api_ok_loop" in job_names
    ok = next(j for j in body["jobs"] if j["name"] == "api_ok_loop")
    # Spec §8: every job carries these fields.
    for f in (
        "schedule_cron",
        "schedule_human",
        "source_kind",
        "description",
        "long_running",
        "last_run_at",
        "last_run_status",
        "last_run_error",
        "next_run_at",
        "currently_running_run_id",
        "health",
    ):
        assert f in ok, f"missing field {f!r} from /api/jobs item"


def test_get_jobs_open_works_without_admin_token_env(
    engine: None, app_without_admin_token
) -> None:
    """Even when ARGOSY_ADMIN_TOKEN is unset, the read-only routes mount."""
    app, tc = app_without_admin_token
    response = tc.get("/api/jobs")
    assert response.status_code == 200, response.text


def test_get_job_detail_returns_404_for_unknown(
    engine: None, app_with_admin_token
) -> None:
    app, tc = app_with_admin_token
    response = tc.get("/api/jobs/nope_not_a_job")
    assert response.status_code == 404
    body = response.json()
    # Spec §8: top-level body (no ``detail`` envelope).
    assert body["error"] == "job_not_found"
    assert body["name"] == "nope_not_a_job"


def test_get_job_detail_happy_path(engine: None, app_with_admin_token) -> None:
    app, tc = app_with_admin_token
    response = tc.get("/api/jobs/api_ok_loop")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metadata"]["name"] == "api_ok_loop"
    assert body["view"]["name"] == "api_ok_loop"
    assert isinstance(body["recent_runs"], list)


# ---------------------------------------------------------------------------
# Admin gate — POST /api/jobs/X/run-now
# ---------------------------------------------------------------------------


def test_run_now_without_header_returns_401(
    engine: None, app_with_admin_token
) -> None:
    app, tc = app_with_admin_token
    response = tc.post("/api/jobs/api_ok_loop/run-now")
    assert response.status_code == 401, response.text
    # Spec §8: top-level ``{"error": "admin_token_required"}``.
    assert response.json() == {"error": "admin_token_required"}


def test_run_now_with_wrong_token_returns_401(
    engine: None, app_with_admin_token
) -> None:
    app, tc = app_with_admin_token
    response = tc.post(
        "/api/jobs/api_ok_loop/run-now",
        headers={"X-Argosy-Admin": "wrong-token"},
    )
    assert response.status_code == 401
    assert response.json() == {"error": "admin_token_invalid"}


def test_run_now_with_valid_token_returns_202(
    engine: None, app_with_admin_token
) -> None:
    app, tc = app_with_admin_token
    response = tc.post(
        "/api/jobs/api_ok_loop/run-now",
        headers={"X-Argosy-Admin": "secret123"},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["name"] == "api_ok_loop"
    assert body["job_run_id"] > 0


def test_run_now_for_unknown_job_returns_404(
    engine: None, app_with_admin_token
) -> None:
    app, tc = app_with_admin_token
    response = tc.post(
        "/api/jobs/no_such_job/run-now",
        headers={"X-Argosy-Admin": "secret123"},
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "job_not_found"
    assert body["name"] == "no_such_job"


# ---------------------------------------------------------------------------
# 409 contention shape — Spec §1.4
# ---------------------------------------------------------------------------


def test_run_now_409_contention_shape(
    engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two run-now requests on the same job — second one MUST get 409 with
    the full §1.4 shape (conflict_reason, job_run_id, lock_holder_state,
    lock_acquired_at, retry_after_s).
    """
    # Build the app with the slow loop registered (holds lock 2s).
    monkeypatch.setenv("ARGOSY_ADMIN_TOKEN", "secret123")
    monkeypatch.delenv("ARGOSY_RUN_SCHEDULER", raising=False)

    from argosy.config import reload_settings

    reload_settings()

    from argosy.api.main import create_app

    app = create_app()
    tc = TestClient(app)
    tc.__enter__()
    try:
        registry: JobRegistry = app.state.job_registry
        scheduler = app.state.scheduler
        slow = _SlowLoop()
        scheduler.register_loop(slow)
        registry.register(job=slow, metadata=_meta("api_slow_loop"))

        # Fire the first request asynchronously via fire_now directly —
        # this holds the lock for 2s and writes a job_runs row.
        first_task = asyncio.get_event_loop().create_task(
            registry.fire_now("api_slow_loop", triggered_by="user:first")
        )

        # Wait until the slow loop's tick body has actually started
        # (lock held, job_runs row open).
        async def _await_started():
            await slow.started.wait()

        asyncio.get_event_loop().run_until_complete(_await_started())

        # Now hit the HTTP endpoint — it should 409 because the lock is held.
        response = tc.post(
            "/api/jobs/api_slow_loop/run-now",
            headers={"X-Argosy-Admin": "secret123"},
        )
        assert response.status_code == 409, response.text
        # Spec §1.4 — top-level body (no ``detail`` envelope).
        body = response.json()
        assert body["error"] == "already_running"
        assert body["conflict_reason"] == "lock_held"
        assert body["job_name"] == "api_slow_loop"
        assert body["lock_holder_state"] == "running"
        assert body["lock_acquired_at"] is not None
        assert body["retry_after_s"] == 5
        # job_run_id may be int or None per §1.4; for "running" it's
        # populated since the audit row is open.
        assert body["job_run_id"] is not None
        assert isinstance(body["job_run_id"], int)

        # Drain the first task so the test teardown is clean.
        asyncio.get_event_loop().run_until_complete(first_task)
    finally:
        tc.__exit__(None, None, None)
        reload_settings()


# ---------------------------------------------------------------------------
# POST /api/jobs/X/stop — 400 for CadenceLoop
# ---------------------------------------------------------------------------


def test_stop_against_cadence_loop_returns_400(
    engine: None, app_with_admin_token
) -> None:
    """``stop`` is LongRunningJob-only; CadenceLoop must 400."""
    app, tc = app_with_admin_token
    response = tc.post(
        "/api/jobs/api_ok_loop/stop",
        headers={"X-Argosy-Admin": "secret123"},
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"] == "not_long_running"
    assert body["name"] == "api_ok_loop"


def test_stop_without_admin_token_returns_401(
    engine: None, app_with_admin_token
) -> None:
    app, tc = app_with_admin_token
    response = tc.post("/api/jobs/api_ok_loop/stop")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/jobs/X/reconnect — LongRunningJob → 202
# ---------------------------------------------------------------------------


def test_reconnect_never_returns_stale_run_id(
    engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex round-2 BLOCKER regression: ``reconnect`` MUST NOT return the
    PREVIOUS cycle's run id as ``new_job_run_id`` when the new supervisor
    cycle hasn't committed its row yet.

    Setup: seed a job_runs row for the long-running job (simulating the
    prior cycle that just got cancelled). Call reconnect with a stub
    that NEVER opens a new row (simulating "supervisor hasn't committed
    its first INSERT yet"). The endpoint MUST return ``new_job_run_id=null``
    — NOT the id of the seed row.
    """
    monkeypatch.setenv("ARGOSY_ADMIN_TOKEN", "secret123")
    monkeypatch.delenv("ARGOSY_RUN_SCHEDULER", raising=False)
    from argosy.config import reload_settings

    reload_settings()
    from argosy.api.main import create_app

    app = create_app()
    tc = TestClient(app)
    tc.__enter__()
    try:
        registry: JobRegistry = app.state.job_registry

        # A stub LongRunningJob whose run() returns immediately with
        # ``clean`` intent — so start_supervisors will spin a supervisor
        # that opens a row and closes it almost instantly. The race we
        # want to exercise is: between cancel_long_running closing the
        # OLD row and the NEW supervisor opening a row, the only
        # job_runs row visible is the OLD one. We seed it explicitly.
        class _FastExitJob(LongRunningJob):
            name = "stale_check_lr"

            def __init__(self) -> None:
                self._exit_intent = "clean"
                self._status = "stopped"

            def connection_status(self) -> str:
                return self._status

            async def run(self) -> None:
                self._exit_intent = "clean"
                return

        lr = _FastExitJob()
        registry.register(
            job=lr, metadata=_meta("stale_check_lr", long_running=True)
        )

        # Seed an OLD row (simulating a prior cycle).
        async def _seed_old():
            async with db_mod.get_session() as session:
                session.add(
                    JobRun(
                        job_name="stale_check_lr",
                        started_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                        finished_at=datetime(2026, 5, 1, 0, 1, tzinfo=timezone.utc),
                        status="cancelled",
                        skip_reason=None,
                        error_message=None,
                        manual_trigger=0,
                        triggered_by="supervisor#0.1",
                        output_summary=None,
                        duration_ms=60000,
                        idempotency_key="seed-old-stale_check_lr",
                    )
                )
                await session.commit()

        asyncio.get_event_loop().run_until_complete(_seed_old())

        # Hit reconnect. The new supervisor's run() returns immediately
        # so it MAY commit a new row before the response builds — that's
        # fine, the assertion below covers both outcomes.
        response = tc.post(
            "/api/jobs/stale_check_lr/reconnect",
            headers={"X-Argosy-Admin": "secret123"},
        )
        assert response.status_code == 202, response.text
        body = response.json()
        new_id = body["new_job_run_id"]

        # The seeded OLD row id must not be returned as the "new" id.
        loop = asyncio.get_event_loop()

        async def _get_seed_id():
            async with db_mod.get_session() as s:
                return (
                    await s.execute(
                        select(JobRun.id).where(
                            JobRun.idempotency_key == "seed-old-stale_check_lr"
                        )
                    )
                ).scalar_one()

        seed_id = loop.run_until_complete(_get_seed_id())
        # ``new_job_run_id`` MUST be either None (no new row yet) OR
        # STRICTLY GREATER than the seed id (a genuinely-new row).
        assert new_id is None or new_id > seed_id, (
            f"reconnect returned new_job_run_id={new_id} but the OLD seeded "
            f"row id is {seed_id} — that's a stale-id leak"
        )
    finally:
        tc.__exit__(None, None, None)
        try:
            asyncio.get_event_loop().run_until_complete(
                app.state.job_registry.stop_supervisors()
            )
        except Exception:
            pass
        reload_settings()


def test_reconnect_ignores_non_supervisor_post_watermark_row(
    engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex round-3 BLOCKER regression: a row inserted by ANOTHER writer
    (e.g. a concurrent ``run-now`` triggered_by='user:admin') AFTER the
    pre-cancel watermark MUST NOT be returned as ``new_job_run_id``.

    The watermark check ``row_id > prev_max_id`` alone is insufficient
    because a concurrent writer's row also passes. The fix additionally
    requires ``triggered_by LIKE 'supervisor#%'`` so only rows from the
    supervisor restart path qualify.
    """
    monkeypatch.setenv("ARGOSY_ADMIN_TOKEN", "secret123")
    monkeypatch.delenv("ARGOSY_RUN_SCHEDULER", raising=False)
    from argosy.config import reload_settings

    reload_settings()
    from argosy.api.main import create_app

    app = create_app()
    tc = TestClient(app)
    tc.__enter__()
    try:
        registry: JobRegistry = app.state.job_registry

        # A stub that NEVER opens a job_runs row from the supervisor
        # path — ``run()`` raises immediately so the supervisor enters
        # backoff before committing anything else, and we control when
        # rows appear via direct DB inserts. This isolates the race
        # under test (foreign post-watermark row vs supervisor row).
        class _NoOpenJob(LongRunningJob):
            name = "race_lr"

            def __init__(self) -> None:
                self._exit_intent = "clean"
                self._status = "stopped"

            def connection_status(self) -> str:
                return self._status

            async def run(self) -> None:
                # Return immediately with ``clean`` intent. The supervisor
                # WILL open a row before run() — we can't stop that. The
                # test instead seeds a foreign row with a higher id so
                # the latest-id query would return that foreign row if
                # the triggered_by filter weren't applied.
                self._exit_intent = "clean"
                return

        lr = _NoOpenJob()
        registry.register(job=lr, metadata=_meta("race_lr", long_running=True))

        loop = asyncio.get_event_loop()

        # Seed a baseline row (the OLD cycle's row).
        async def _seed_baseline():
            async with db_mod.get_session() as s:
                s.add(
                    JobRun(
                        job_name="race_lr",
                        started_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                        finished_at=datetime(
                            2026, 5, 1, 0, 1, tzinfo=timezone.utc
                        ),
                        status="cancelled",
                        skip_reason=None,
                        error_message=None,
                        manual_trigger=0,
                        triggered_by="supervisor#0.1",
                        output_summary=None,
                        duration_ms=60000,
                        idempotency_key="race-baseline",
                    )
                )
                await s.commit()

        loop.run_until_complete(_seed_baseline())

        # The reconnect call's flow:
        #  1. Snapshot prev_max_id = baseline id.
        #  2. cancel_long_running() — registers a clean exit; no row
        #     opens because no supervisor is currently running.
        #  3. start_supervisors() — spins a fresh supervisor.
        # We exercise the race by patching cancel_long_running to
        # INJECT a foreign row AFTER step 1 (post-watermark) but BEFORE
        # the post-restart selection query at step 4. Without the
        # triggered_by filter the foreign id (id > prev_max_id) would
        # satisfy the guard; the filter rules it out.
        async def _inject_foreign():
            async with db_mod.get_session() as s:
                s.add(
                    JobRun(
                        job_name="race_lr",
                        started_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
                        finished_at=None,
                        status="running",
                        skip_reason=None,
                        error_message=None,
                        manual_trigger=1,
                        triggered_by="user:admin",  # NOT supervisor#...
                        output_summary=None,
                        duration_ms=None,
                        idempotency_key="race-foreign",
                    )
                )
                await s.commit()

        # Patch cancel_long_running so the foreign row is committed
        # AFTER the watermark snapshot has been taken (the watermark
        # snapshot is the first DB read in the reconnect handler;
        # cancel_long_running is the second step).
        orig_cancel = registry.cancel_long_running

        async def _patched_cancel(name_: str):
            await _inject_foreign()
            await orig_cancel(name_)

        monkeypatch.setattr(
            registry, "cancel_long_running", _patched_cancel
        )

        response = tc.post(
            "/api/jobs/race_lr/reconnect",
            headers={"X-Argosy-Admin": "secret123"},
        )
        assert response.status_code == 202, response.text
        new_id = response.json()["new_job_run_id"]

        # Identify the foreign row id; new_id MUST NOT equal it.
        async def _ids():
            async with db_mod.get_session() as s:
                foreign = (
                    await s.execute(
                        select(JobRun.id).where(
                            JobRun.idempotency_key == "race-foreign"
                        )
                    )
                ).scalar_one()
                # Best-effort supervisor row from the fresh cycle:
                sup = (
                    await s.execute(
                        select(JobRun.id)
                        .where(JobRun.job_name == "race_lr")
                        .where(JobRun.triggered_by.like("supervisor#%"))
                        .where(
                            JobRun.idempotency_key != "race-baseline"
                        )
                        .order_by(JobRun.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                return foreign, sup

        foreign_id, sup_id = loop.run_until_complete(_ids())

        # The new_id is either None (no supervisor row yet) or the
        # supervisor's row — never the foreign run-now row.
        assert new_id != foreign_id, (
            f"reconnect leaked the foreign (non-supervisor) row id "
            f"{foreign_id} as new_job_run_id={new_id}"
        )
        if new_id is not None:
            assert new_id == sup_id, (
                f"new_job_run_id={new_id} did not match the supervisor "
                f"row id {sup_id}"
            )
    finally:
        tc.__exit__(None, None, None)
        try:
            asyncio.get_event_loop().run_until_complete(
                app.state.job_registry.stop_supervisors()
            )
        except Exception:
            pass
        reload_settings()


def test_reconnect_long_running_returns_202(
    engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARGOSY_ADMIN_TOKEN", "secret123")
    monkeypatch.delenv("ARGOSY_RUN_SCHEDULER", raising=False)
    from argosy.config import reload_settings

    reload_settings()
    from argosy.api.main import create_app

    app = create_app()
    tc = TestClient(app)
    tc.__enter__()
    try:
        registry: JobRegistry = app.state.job_registry
        lr = _StubLongRunning()
        registry.register(
            job=lr,
            metadata=_meta("api_longrunner", long_running=True),
        )
        # Start the supervisor so there's a live cycle to reconnect against.
        asyncio.get_event_loop().run_until_complete(registry.start_supervisors())

        # Give the supervisor a moment to open the first job_runs row.
        async def _let_supervisor_open():
            for _ in range(50):
                async with db_mod.get_session() as s:
                    row_id = (
                        await s.execute(
                            JobRun.__table__.select().where(
                                JobRun.job_name == "api_longrunner"
                            )
                        )
                    ).first()
                    if row_id is not None:
                        return
                await asyncio.sleep(0.02)

        asyncio.get_event_loop().run_until_complete(_let_supervisor_open())

        response = tc.post(
            "/api/jobs/api_longrunner/reconnect",
            headers={"X-Argosy-Admin": "secret123"},
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["name"] == "api_longrunner"
        # ``new_job_run_id`` is either the id of the freshly-opened cycle
        # OR ``None`` if the new supervisor hasn't quite opened its row
        # yet (race window — the UI polls /api/jobs/{name} shortly after).
        assert body["new_job_run_id"] is None or isinstance(
            body["new_job_run_id"], int
        )
    finally:
        tc.__exit__(None, None, None)
        # Allow async teardown.
        try:
            asyncio.get_event_loop().run_until_complete(
                app.state.job_registry.stop_supervisors()
            )
        except Exception:
            pass
        reload_settings()


# ---------------------------------------------------------------------------
# Mutating routes REFUSE to mount when env unset (Spec §Commit #4 BLOCKER #1)
# ---------------------------------------------------------------------------


def test_mutating_routes_refuse_to_mount_without_env(
    engine: None, app_without_admin_token
) -> None:
    """When ``ARGOSY_ADMIN_TOKEN`` is unset, the POST routes are NOT
    registered. We expect 404 (route absent) — NOT 401 (route present
    but auth failed). Spec §Commit #4 BLOCKER #1 explicitly distinguishes
    these: 404 is the operator's signal that the gate isn't configured.
    """
    app, tc = app_without_admin_token
    # GET still works.
    assert tc.get("/api/jobs").status_code == 200

    # All three POSTs must 404 (route not mounted), even with a header.
    for path in (
        "/api/jobs/api_ok_loop/run-now",
        "/api/jobs/api_ok_loop/stop",
        "/api/jobs/api_ok_loop/reconnect",
    ):
        r = tc.post(path, headers={"X-Argosy-Admin": "anything"})
        assert r.status_code == 404, (
            f"{path} should be ABSENT when ARGOSY_ADMIN_TOKEN unset; "
            f"got {r.status_code}: {r.text}"
        )


# ---------------------------------------------------------------------------
# GET /api/jobs/{name}/runs — limit + include_skipped
# ---------------------------------------------------------------------------


def _seed_run_rows(job_name: str, *, n_ok: int = 5, n_skipped: int = 2) -> None:
    """Insert ``n_ok`` status='ok' rows + ``n_skipped`` status='skipped' rows."""

    async def _insert() -> None:
        async with db_mod.get_session() as session:
            for i in range(n_ok):
                session.add(
                    JobRun(
                        job_name=job_name,
                        started_at=datetime(2026, 5, 1, 12, i, 0, tzinfo=timezone.utc),
                        finished_at=datetime(
                            2026, 5, 1, 12, i, 1, tzinfo=timezone.utc
                        ),
                        status="ok",
                        skip_reason=None,
                        error_message=None,
                        manual_trigger=0,
                        triggered_by="scheduler",
                        output_summary=None,
                        duration_ms=1000,
                        idempotency_key=f"seed-ok-{job_name}-{i}",
                    )
                )
            for i in range(n_skipped):
                session.add(
                    JobRun(
                        job_name=job_name,
                        started_at=datetime(2026, 5, 2, 12, i, 0, tzinfo=timezone.utc),
                        finished_at=datetime(
                            2026, 5, 2, 12, i, 1, tzinfo=timezone.utc
                        ),
                        status="skipped",
                        skip_reason="market_closed",
                        error_message=None,
                        manual_trigger=0,
                        triggered_by="scheduler",
                        output_summary=None,
                        duration_ms=0,
                        idempotency_key=f"seed-skipped-{job_name}-{i}",
                    )
                )
            await session.commit()

    asyncio.get_event_loop().run_until_complete(_insert())


def test_get_runs_limit_caps_rows(
    engine: None, app_with_admin_token
) -> None:
    app, tc = app_with_admin_token
    _seed_run_rows("api_ok_loop", n_ok=10, n_skipped=0)

    response = tc.get("/api/jobs/api_ok_loop/runs?limit=5")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["runs"]) <= 5
    assert body["has_more"] is True


def test_get_runs_excludes_skipped_by_default(
    engine: None, app_with_admin_token
) -> None:
    app, tc = app_with_admin_token
    _seed_run_rows("api_ok_loop", n_ok=3, n_skipped=2)

    response = tc.get("/api/jobs/api_ok_loop/runs?limit=20")
    assert response.status_code == 200
    statuses = [r["status"] for r in response.json()["runs"]]
    assert "skipped" not in statuses
    assert statuses.count("ok") == 3


def test_get_runs_include_skipped_true_includes_them(
    engine: None, app_with_admin_token
) -> None:
    app, tc = app_with_admin_token
    _seed_run_rows("api_ok_loop", n_ok=3, n_skipped=2)

    response = tc.get(
        "/api/jobs/api_ok_loop/runs?limit=20&include_skipped=true"
    )
    assert response.status_code == 200
    statuses = [r["status"] for r in response.json()["runs"]]
    assert statuses.count("skipped") == 2
    assert statuses.count("ok") == 3


def test_get_runs_limit_max_clamps_to_200(
    engine: None, app_with_admin_token
) -> None:
    """``limit`` is bounded [1, 200] by the Query validator. 201 → 422."""
    app, tc = app_with_admin_token
    response = tc.get("/api/jobs/api_ok_loop/runs?limit=201")
    assert response.status_code == 422


def test_get_runs_for_unknown_job_returns_404(
    engine: None, app_with_admin_token
) -> None:
    app, tc = app_with_admin_token
    response = tc.get("/api/jobs/no_such_job/runs")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Sprint A commit #9 extension — skip_reason surfaces in the response body
# when include_skipped=true so the UI can render "why didn't this fire?"
# (Spec §8 — JobRunRow shape includes skip_reason; IMPORTANT #4).
# ---------------------------------------------------------------------------


def test_get_runs_include_skipped_surfaces_skip_reason(
    engine: None, app_with_admin_token
) -> None:
    """When ``include_skipped=true`` the response rows MUST carry the
    ``skip_reason`` so the UI can show "market_closed" / "throttle" /
    etc. instead of a bare ``status='skipped'``.
    """
    app, tc = app_with_admin_token
    _seed_run_rows("api_ok_loop", n_ok=0, n_skipped=2)

    response = tc.get(
        "/api/jobs/api_ok_loop/runs?limit=20&include_skipped=true"
    )
    assert response.status_code == 200, response.text
    runs = response.json()["runs"]
    skipped_runs = [r for r in runs if r["status"] == "skipped"]
    assert len(skipped_runs) == 2
    for row in skipped_runs:
        # Spec §8 / §A.JobRunRow — every skipped row carries a non-null
        # skip_reason. The seed helper uses ``market_closed``.
        assert row["skip_reason"] == "market_closed", (
            f"skipped row missing skip_reason: {row!r}"
        )
        # Mirror invariant: non-skipped rows below should have null
        # skip_reason (sanity that the field round-trips both ways).
    for row in runs:
        if row["status"] != "skipped":
            assert row["skip_reason"] is None, (
                f"non-skipped row had skip_reason set: {row!r}"
            )
