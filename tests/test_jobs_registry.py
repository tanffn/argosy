"""Sprint A commit #3a — ``JobRegistry`` shell + ``RegisteredScheduler``.

Spec: ``docs/superpowers/specs/2026-05-29-jobs-registry-design.md`` §1.3,
§1.4, §1.6, §1.7.

Test surface (mirrors the prompt's commit #3a checklist):

* happy-path register + ``fire_now`` → row in ``job_runs`` with
  ``status='ok'``
* error path: ``loop.tick`` raises → row written with ``status='error'``,
  exception re-raised
* lock contention 409 (`AlreadyRunning`): simultaneous ``fire_now`` calls
  → second one raises with ``job_run_id`` + ``lock_holder_state='running'``
* dual-write ordering (codex BLOCKER #4): failure injection on
  ``_record_tick`` → ``job_runs`` row already in ``status='ok'``
* single-writer assertion (codex BLOCKER #2): ripgrep walks ``argosy/``
  for INSERT INTO job_runs / JobRun( and asserts only the registry
  module is matched
* idempotency_key UNIQUE: simulated transport blip during
  ``_open_job_run`` → only one row written

Test command::

    D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest \\
        -m "not llm_eval" tests/test_jobs_registry.py -v
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs import (
    AlreadyRunning,
    JobMetadata,
    JobRegistry,
    RegisteredScheduler,
)
from argosy.services.jobs.registry import _idempotency_key, _derive_health, JobView
from argosy.services.jobs.retry import RetryConfig, retry_transient
from argosy.state import db as db_mod
from argosy.state.models import CadenceState, JobRun


# ---------------------------------------------------------------------------
# Test loops
# ---------------------------------------------------------------------------


class _OkLoop(CadenceLoop):
    name = "ok_loop"

    def __init__(self) -> None:
        super().__init__(schedule=LoopSchedule(interval_seconds=60), enabled=True)
        self.tick_count = 0
        self.last_output_summary = None

    async def tick(self, *, now=None):  # type: ignore[override]
        self.tick_count += 1
        self.last_output_summary = {"counts": {"ticks": self.tick_count}}


class _RaiseLoop(CadenceLoop):
    name = "raise_loop"

    def __init__(self) -> None:
        super().__init__(schedule=LoopSchedule(interval_seconds=60), enabled=True)

    async def tick(self, *, now=None):  # type: ignore[override]
        raise RuntimeError("boom")


class _SlowLoop(CadenceLoop):
    """Holds the lock for `hold_s` seconds so a competing fire_now 409s."""

    name = "slow_loop"

    def __init__(self, *, hold_s: float = 1.5) -> None:
        super().__init__(schedule=LoopSchedule(interval_seconds=60), enabled=True)
        self._hold_s = hold_s
        self.started = asyncio.Event()

    async def tick(self, *, now=None):  # type: ignore[override]
        self.started.set()
        await asyncio.sleep(self._hold_s)


def _meta(name: str, **overrides) -> JobMetadata:
    defaults = dict(
        name=name,
        schedule_cron=None,
        schedule_human="every 60s",
        source_kind="maintenance",
        description=f"test fixture {name}",
        long_running=False,
        lock_acquire_timeout_s=1.0,
    )
    defaults.update(overrides)
    return JobMetadata(**defaults)


def _build_registry_and_scheduler(*loops_with_meta):
    """Wire registry + RegisteredScheduler + register the loops.

    Returns (registry, scheduler). Each `loops_with_meta` element is a
    (loop, metadata) tuple.
    """
    registry = JobRegistry()
    # AgentSettings is lazily loaded by Scheduler.__init__; pass a tiny
    # stand-in via a custom subclass to avoid YAML round-trip cost.
    scheduler = RegisteredScheduler(
        user_id="ariel",
        settings=_DummySettings(),
        registry=registry,
    )
    registry.bind_scheduler(scheduler)
    for loop, meta in loops_with_meta:
        scheduler.register_loop(loop)
        registry.register(job=loop, metadata=meta)
    return registry, scheduler


class _DummySettings:
    """Minimal AgentSettings stand-in for tests.

    Scheduler only reads ``settings.cadences.<name>`` inside
    ``register_default_loops``, which our tests don't call.
    """


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_now_happy_path(engine: None) -> None:
    loop = _OkLoop()
    registry, _ = _build_registry_and_scheduler((loop, _meta("ok_loop")))

    run_id = await registry.fire_now("ok_loop", triggered_by="user:ariel")

    assert run_id > 0
    assert loop.tick_count == 1

    async with db_mod.get_session() as s:
        row = (
            await s.execute(select(JobRun).where(JobRun.id == run_id))
        ).scalar_one()
        assert row.job_name == "ok_loop"
        assert row.status == "ok"
        assert row.manual_trigger == 1
        assert row.triggered_by == "user:ariel"
        assert row.error_message is None
        assert row.duration_ms is not None
        assert row.duration_ms >= 0
        # output_summary is JSON-in-TEXT with the loop's counts.
        assert row.output_summary is not None
        assert '"ticks": 1' in row.output_summary

        # cadence_state was written too (parent's _record_tick fired
        # AFTER _close_job_run — order test is below).
        cad = (
            await s.execute(
                select(CadenceState).where(CadenceState.loop_name == "ok_loop")
            )
        ).scalar_one()
        assert cad.last_status == "ok"


@pytest.mark.asyncio
async def test_scheduled_path_uses_same_writer(engine: None) -> None:
    """The scheduled path (no manual context) records a row too."""
    loop = _OkLoop()
    _, scheduler = _build_registry_and_scheduler((loop, _meta("ok_loop")))

    # Drive the scheduled path by calling _fire_once directly — that's
    # what _run_loop does after sleeping.
    await scheduler._fire_once(loop)

    async with db_mod.get_session() as s:
        row = (
            await s.execute(
                select(JobRun).where(JobRun.job_name == "ok_loop")
            )
        ).scalar_one()
        assert row.status == "ok"
        assert row.manual_trigger == 0
        assert row.triggered_by == "scheduler"


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_now_error_path(engine: None) -> None:
    loop = _RaiseLoop()
    registry, _ = _build_registry_and_scheduler((loop, _meta("raise_loop")))

    with pytest.raises(RuntimeError, match="boom"):
        await registry.fire_now("raise_loop", triggered_by="user:test")

    async with db_mod.get_session() as s:
        row = (
            await s.execute(
                select(JobRun).where(JobRun.job_name == "raise_loop")
            )
        ).scalar_one()
        assert row.status == "error"
        assert row.error_message == "boom"
        assert row.manual_trigger == 1


# ---------------------------------------------------------------------------
# Lock contention — codex IMPORTANT #1 (409 shape)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduled_path_acquires_lock(engine: None) -> None:
    """Scheduled ``_fire_once`` MUST hold the per-job lock for the tick
    body (codex BLOCKER — addresses §1.4 lock contract).

    Sanity: while a scheduled tick is mid-flight, the lock for that
    job is locked. We inspect lock state from inside the loop's tick.
    """
    observed: dict[str, bool | None] = {"locked_during_tick": None}

    class _ObserverLoop(CadenceLoop):
        name = "observer_loop"

        def __init__(self) -> None:
            super().__init__(
                schedule=LoopSchedule(interval_seconds=60), enabled=True
            )
            self.last_output_summary = None

        async def tick(self_inner, *, now=None):  # type: ignore[override]
            # Inspect the registry's lock for this job from inside tick.
            observed["locked_during_tick"] = registry._lock_for(
                "observer_loop"
            ).locked()

    loop = _ObserverLoop()
    registry, scheduler = _build_registry_and_scheduler(
        (loop, _meta("observer_loop"))
    )

    await scheduler._fire_once(loop)
    assert observed["locked_during_tick"] is True
    # And released after the tick.
    assert registry._lock_for("observer_loop").locked() is False


@pytest.mark.asyncio
async def test_fire_now_lock_contention_raises_already_running(
    engine: None,
) -> None:
    loop = _SlowLoop(hold_s=2.0)
    registry, _ = _build_registry_and_scheduler(
        (loop, _meta("slow_loop", lock_acquire_timeout_s=0.2))
    )

    # Kick off the first manual trigger; it'll hold the lock for ~2s.
    first = asyncio.create_task(registry.fire_now("slow_loop"))
    # Wait until the slow loop's tick body has actually started so we
    # know the lock is held + the audit row was opened.
    await loop.started.wait()

    with pytest.raises(AlreadyRunning) as exc_info:
        await registry.fire_now("slow_loop", triggered_by="user:second")

    err = exc_info.value
    assert err.job_name == "slow_loop"
    assert err.job_run_id is not None
    assert err.lock_holder_state == "running"
    assert err.lock_acquired_at is not None
    assert err.retry_after_s == 5

    body = err.to_response_body()
    assert body["error"] == "already_running"
    assert body["conflict_reason"] == "lock_held"
    assert body["lock_holder_state"] == "running"

    # Let the first call complete so the test cleans up.
    first_run_id = await first
    assert err.job_run_id == first_run_id


# ---------------------------------------------------------------------------
# Dual-write ordering — codex BLOCKER #4
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dual_write_ordering_close_before_record_tick(
    engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject failure into ``_record_tick``; the audit row must already be
    finalized to ``ok`` by the time ``_record_tick`` runs.
    """
    loop = _OkLoop()
    registry, scheduler = _build_registry_and_scheduler(
        (loop, _meta("ok_loop"))
    )

    observed: dict[str, str | None] = {"status_at_record_tick": None}

    async def _failing_record_tick(self, loop_name, *, status, error, next_due=None):
        # Inspect the audit row at the moment _record_tick is called.
        async with db_mod.get_session() as s:
            row = (
                await s.execute(
                    select(JobRun).where(JobRun.job_name == loop_name)
                )
            ).scalar_one_or_none()
            observed["status_at_record_tick"] = row.status if row else None
        raise RuntimeError("simulated cadence_state failure")

    monkeypatch.setattr(
        RegisteredScheduler, "_record_tick", _failing_record_tick
    )

    with pytest.raises(RuntimeError, match="simulated cadence_state failure"):
        await scheduler._fire_once(loop, force=True)

    # Audit row's status was already 'ok' BEFORE _record_tick fired.
    assert observed["status_at_record_tick"] == "ok"


# ---------------------------------------------------------------------------
# Single-writer enforcement — codex BLOCKER #2
# ---------------------------------------------------------------------------


def test_job_runs_single_writer() -> None:
    """Walk ``argosy/`` and assert only the registry module writes to
    ``job_runs`` (codex BLOCKER #2).

    Allowed writers: only ``argosy/services/jobs/registry.py``. The
    supervisor for LongRunningJob (commit #5) lives in the SAME module
    so the contract extends naturally.

    Three regex sweeps:

    1. ``INSERT INTO job_runs`` — raw SQL writes.
    2. ``JobRun(...)`` constructor (anchored, no false-positive on
       ``JobRunResult``/``JobRunRow``).
    3. ``._open_job_run(`` / ``._close_job_run(`` — helper-call boundary
       (codex IMPORTANT — confirms no caller bypasses the writer module
       even via the helper).

    Files enumerated via ``git ls-files`` UNION ``rglob("*.py")`` so a
    new-but-not-yet-staged offender is still caught (codex IMPORTANT).
    """
    repo_root = Path(__file__).resolve().parent.parent
    argosy_root = repo_root / "argosy"

    file_set: set[Path] = set()
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "argosy"], cwd=repo_root, text=True
        )
        for line in out.splitlines():
            line = line.strip()
            if line.endswith(".py"):
                file_set.add((repo_root / line).resolve())
    except (FileNotFoundError, subprocess.CalledProcessError):  # pragma: no cover
        pass
    # Union with rglob to catch untracked new files.
    for p in argosy_root.rglob("*.py"):
        file_set.add(p.resolve())

    files = sorted(file_set)

    # Pattern 1: raw SQL.
    insert_pattern = re.compile(r"INSERT\s+INTO\s+job_runs", re.IGNORECASE)
    # Pattern 2: ORM constructor — anchored with negative-lookbehind to
    # avoid matching names like JobRunResult or JobRunRow.
    constructor_pattern = re.compile(r"(?<![A-Za-z0-9_])JobRun\s*\(")
    # Pattern 3: helper-call boundary.
    helper_pattern = re.compile(r"\._(open|close)_job_run\s*\(")

    allowed_writers = {
        (argosy_root / "services" / "jobs" / "registry.py").resolve(),
    }
    # The ORM class definition itself + the seam that USES the helpers
    # are explicitly allowed for the relevant patterns only.
    allowed_constructor = allowed_writers | {
        (argosy_root / "state" / "models.py").resolve(),
    }
    allowed_helper_callers = allowed_writers | {
        (argosy_root / "services" / "jobs" / "registered_scheduler.py").resolve(),
    }

    offenders: list[tuple[str, str, str]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            stripped = line.strip()
            # Skip comments + docstring-y lines to reduce false positives.
            if stripped.startswith("#"):
                continue
            if insert_pattern.search(line) and path not in allowed_writers:
                offenders.append((str(path.relative_to(repo_root)),
                                  "INSERT-INTO", stripped))
                break
            if constructor_pattern.search(line) and path not in allowed_constructor:
                offenders.append((str(path.relative_to(repo_root)),
                                  "JobRun(", stripped))
                break
            if helper_pattern.search(line) and path not in allowed_helper_callers:
                offenders.append((str(path.relative_to(repo_root)),
                                  "_open/_close_job_run", stripped))
                break

    assert not offenders, (
        "BLOCKER #2 violated — only argosy/services/jobs/registry.py is "
        "allowed to write to job_runs; only registered_scheduler.py may "
        "call the open/close helpers.\nOffending lines:\n"
        + "\n".join(f"  {p} [{kind}]: {ln}" for p, kind, ln in offenders)
    )


# ---------------------------------------------------------------------------
# Idempotency-key UNIQUE under retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_job_run_idempotency_key_dedups_on_retry(
    engine: None,
) -> None:
    """Two ``_open_job_run`` calls with the same ``(job_name, started_at,
    triggered_by)`` produce ONE row + the second returns the first's id.

    Mirrors the §1.7 "transport blip during _open_job_run" scenario.
    """
    registry = JobRegistry()
    started = datetime(2026, 5, 29, 14, 0, 0, tzinfo=timezone.utc)

    run_id_1 = await registry._open_job_run(
        job_name="dedup_job",
        manual_trigger=False,
        triggered_by="scheduler",
        started_at=started,
    )
    run_id_2 = await registry._open_job_run(
        job_name="dedup_job",
        manual_trigger=False,
        triggered_by="scheduler",
        started_at=started,
    )

    assert run_id_1 == run_id_2

    async with db_mod.get_session() as s:
        rows = (
            await s.execute(
                select(JobRun).where(JobRun.job_name == "dedup_job")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == run_id_1
        # idempotency_key matches the documented formula.
        expected_key = _idempotency_key("dedup_job", started, "scheduler")
        assert rows[0].idempotency_key == expected_key


# ---------------------------------------------------------------------------
# Retry helper — transport-only classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_transient_retries_on_timeout() -> None:
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.TimeoutError("transport blip")
        return "ok"

    out = await retry_transient(
        flaky, attempts=2, jitter_min_s=0.0, jitter_max_s=0.0
    )
    assert out == "ok"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_retry_transient_does_not_retry_business_errors() -> None:
    calls = {"n": 0}

    async def biz() -> None:
        calls["n"] += 1
        raise ValueError("bad input")

    with pytest.raises(ValueError, match="bad input"):
        await retry_transient(biz, attempts=3, jitter_min_s=0.0, jitter_max_s=0.0)
    # Only one call — business errors hard-fail.
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retry_transient_exhausts_and_reraises() -> None:
    async def always_blip() -> None:
        raise asyncio.TimeoutError("always")

    with pytest.raises(asyncio.TimeoutError):
        await retry_transient(
            always_blip, attempts=2, jitter_min_s=0.0, jitter_max_s=0.0
        )


def test_retry_config_default_and_no_retry() -> None:
    """``RetryConfig.DEFAULT`` is the 1-retry-with-jitter shape;
    ``no_retry()`` is the opt-out.
    """
    default = RetryConfig.DEFAULT  # type: ignore[attr-defined]
    assert default.attempts == 2
    assert default.jitter_min_s == pytest.approx(0.5)
    assert default.jitter_max_s == pytest.approx(2.0)

    nope = RetryConfig.no_retry()
    assert nope.attempts == 1


# ---------------------------------------------------------------------------
# Health derivation table (§1.6) — sanity checks
# ---------------------------------------------------------------------------


def test_derive_health_ok_fresh() -> None:
    now = datetime.now(timezone.utc)
    view = JobView(
        metadata=_meta("h"),
        last_run_at=now,
        last_run_status="ok",
    )
    assert _derive_health(view, cadence_seconds=3600) == "green"


def test_derive_health_ok_stale() -> None:
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    view = JobView(
        metadata=_meta("h"),
        last_run_at=now - timedelta(hours=3),
        last_run_status="ok",
    )
    # cadence is 1h; last run is 3h ago = > 2× cadence → amber.
    assert _derive_health(view, cadence_seconds=3600) == "amber"


def test_derive_health_error_is_red() -> None:
    view = JobView(
        metadata=_meta("h"),
        last_run_at=datetime.now(timezone.utc),
        last_run_status="error",
    )
    assert _derive_health(view, cadence_seconds=3600) == "red"


def test_derive_health_running_recent_green_old_amber() -> None:
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    fresh = JobView(
        metadata=_meta("h"),
        last_run_at=now,
        last_run_status="running",
    )
    assert _derive_health(fresh, cadence_seconds=3600) == "green"

    old = JobView(
        metadata=_meta("h"),
        last_run_at=now - timedelta(minutes=15),
        last_run_status="running",
    )
    assert _derive_health(old, cadence_seconds=3600) == "amber"


def test_derive_health_longrunning_states() -> None:
    """LongRunningJob branch — connected/reconnecting/stopped."""
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    connected = JobView(
        metadata=_meta("h", long_running=True),
        last_run_at=now,
        last_run_status="connected",
    )
    assert _derive_health(connected) == "green"

    reconnecting_fresh = JobView(
        metadata=_meta("h", long_running=True),
        last_run_at=now - timedelta(seconds=10),
        last_run_status="reconnecting",
    )
    assert _derive_health(reconnecting_fresh) == "amber"

    reconnecting_stale = JobView(
        metadata=_meta("h", long_running=True),
        last_run_at=now - timedelta(seconds=120),
        last_run_status="reconnecting",
    )
    assert _derive_health(reconnecting_stale) == "red"

    stopped = JobView(
        metadata=_meta("h", long_running=True),
        last_run_status="stopped",
    )
    assert _derive_health(stopped) == "red"


# ---------------------------------------------------------------------------
# Registration sanity
# ---------------------------------------------------------------------------


def test_register_rejects_name_mismatch() -> None:
    loop = _OkLoop()
    reg = JobRegistry()
    with pytest.raises(ValueError, match="must match"):
        reg.register(job=loop, metadata=_meta("not_ok_loop"))


def test_register_rejects_duplicate() -> None:
    reg = JobRegistry()
    reg.register(job=_OkLoop(), metadata=_meta("ok_loop"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(job=_OkLoop(), metadata=_meta("ok_loop"))


def test_fire_now_requires_bound_scheduler() -> None:
    reg = JobRegistry()
    reg.register(job=_OkLoop(), metadata=_meta("ok_loop"))

    async def go():
        await reg.fire_now("ok_loop")

    with pytest.raises(RuntimeError, match="no scheduler bound"):
        asyncio.run(go())


def test_idempotency_key_formula() -> None:
    started = datetime(2026, 5, 29, 14, 0, 5, tzinfo=timezone.utc)
    key = _idempotency_key("news_daily", started, "user:ariel")
    assert key == "news_daily|2026-05-29T14:00:05+00:00|user:ariel"


# ---------------------------------------------------------------------------
# Supervisor placeholders raise NotImplementedError (commit #5 lands them)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_job_run_failure_still_records_tick(
    engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §1.7 matrix row "close fails, record_tick ok".

    If ``_close_job_run`` raises, the override must still attempt
    ``_record_tick`` so the cadence pointer doesn't get stuck. The
    audit row remains in 'running' (reaped later); the cadence_state
    row reflects the successful tick.
    """
    loop = _OkLoop()
    registry, scheduler = _build_registry_and_scheduler(
        (loop, _meta("ok_loop"))
    )

    async def _failing_close(self, run_id, *, status, **kwargs):
        raise RuntimeError("simulated close write failure")

    monkeypatch.setattr(JobRegistry, "_close_job_run", _failing_close)

    # Should NOT raise — close failure is swallowed + logged.
    await scheduler._fire_once(loop)

    async with db_mod.get_session() as s:
        # Audit row stuck in 'running' (would be reaped by retention).
        row = (
            await s.execute(
                select(JobRun).where(JobRun.job_name == "ok_loop")
            )
        ).scalar_one()
        assert row.status == "running"
        # cadence_state advanced because _record_tick fired despite the
        # close failure.
        cad = (
            await s.execute(
                select(CadenceState).where(CadenceState.loop_name == "ok_loop")
            )
        ).scalar_one()
        assert cad.last_status == "ok"


@pytest.mark.asyncio
async def test_fire_now_holds_lock_through_dispatch(engine: None) -> None:
    """Round-2 codex BLOCKER fix: ``fire_now`` MUST hold the lock for
    the FULL dispatch — no release-then-reacquire window.

    We inspect the lock from inside the tick body during a manual
    fire. The lock is locked AND the audit row's ``manual_trigger`` /
    ``triggered_by`` are correctly stamped (i.e. a hypothetical
    racing scheduled tick could not have mislabeled them).
    """
    observed: dict[str, bool | None] = {"locked": None}

    class _Probe(CadenceLoop):
        name = "probe"

        def __init__(self) -> None:
            super().__init__(
                schedule=LoopSchedule(interval_seconds=60), enabled=True
            )
            self.last_output_summary = None

        async def tick(self_inner, *, now=None):  # type: ignore[override]
            observed["locked"] = registry._lock_for("probe").locked()

    loop = _Probe()
    registry, _ = _build_registry_and_scheduler((loop, _meta("probe")))

    run_id = await registry.fire_now("probe", triggered_by="user:racey")
    assert observed["locked"] is True

    async with db_mod.get_session() as s:
        row = (
            await s.execute(select(JobRun).where(JobRun.id == run_id))
        ).scalar_one()
        assert row.manual_trigger == 1
        assert row.triggered_by == "user:racey"


@pytest.mark.asyncio
async def test_no_release_reacquire_race_on_manual_fire(engine: None) -> None:
    """Two ``fire_now`` calls racing for the same job — the second
    must observe the first holder's ``triggered_by`` via the 409,
    NOT inherit it onto its own audit row.

    Previously the manual path released the lock before delegating,
    creating a window where a second manual caller could stage its
    own ``_manual_context`` and the in-flight tick would read the
    wrong value. The new single-acquire model eliminates this.
    """
    loop = _SlowLoop(hold_s=1.5)
    registry, _ = _build_registry_and_scheduler(
        (loop, _meta("slow_loop", lock_acquire_timeout_s=0.2))
    )

    first = asyncio.create_task(
        registry.fire_now("slow_loop", triggered_by="user:first")
    )
    await loop.started.wait()

    # Second call: lock is held by `first`; the 0.2s timeout fires.
    with pytest.raises(AlreadyRunning) as exc_info:
        await registry.fire_now("slow_loop", triggered_by="user:second")

    # The 409 shape carries the FIRST caller's holder state.
    assert exc_info.value.lock_holder_state == "running"

    first_run_id = await first
    async with db_mod.get_session() as s:
        rows = (
            await s.execute(
                select(JobRun).where(JobRun.job_name == "slow_loop")
            )
        ).scalars().all()
        # Exactly ONE row was written (the first call's). The second
        # call's `AlreadyRunning` raised BEFORE any audit work.
        assert len(rows) == 1
        assert rows[0].id == first_run_id
        # And it's labeled with the first caller's trigger label,
        # NOT the second's — race regression caught.
        assert rows[0].triggered_by == "user:first"


def test_fire_once_matches_parent_body() -> None:
    """Structural parity check (referenced by registered_scheduler.py docstring).

    The ``loop.tick(now=self.clock)`` call must be the IDENTICAL
    invocation the parent Scheduler uses (scheduler.py:282). If the
    parent grows extra steps, this test forces the override to follow.
    """
    import inspect

    from argosy.orchestrator.scheduler import Scheduler
    from argosy.services.jobs.registered_scheduler import RegisteredScheduler

    parent_src = inspect.getsource(Scheduler._fire_once)
    locked = inspect.getsource(RegisteredScheduler.fire_once_already_locked)

    # Both must contain the exact tick invocation token sequence.
    assert "await loop.tick(now=self.clock)" in parent_src
    assert "await loop.tick(now=self.clock)" in locked, (
        "Override must call loop.tick with the same shape as the parent."
    )

    # Both must use _record_tick with the same TickStatus enum.
    assert "TickStatus.OK" in locked
    assert "TickStatus.ERROR" in locked


@pytest.mark.asyncio
async def test_supervisor_lifecycle_with_no_long_running_jobs(
    engine: None,
) -> None:
    """Commit #5 — start/stop_supervisors are idempotent no-ops when no
    LongRunningJob is registered. cancel_long_running of an unknown
    name raises KeyError (NOT NotImplementedError — the placeholder
    semantics changed when commit #5 landed).
    """
    reg = JobRegistry()
    # start/stop with no LongRunningJob registered — pure no-ops.
    await reg.start_supervisors()
    await reg.stop_supervisors()

    # Unknown name raises KeyError, not NotImplementedError.
    with pytest.raises(KeyError):
        await reg.cancel_long_running("nonexistent")


# ---------------------------------------------------------------------------
# Sprint A commit #9 extensions — lock_holder_state coverage, supervisor
# backoff exponential growth, exit_intent='clean' does NOT auto-restart.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_now_409_lock_holder_state_running(engine: None) -> None:
    """``lock_holder_state='running'`` — the in-memory holder marker has
    been flipped to ``running`` by ``_open_job_run``'s ``_note_holder``
    call. The slow loop's tick has started, so by the time the second
    ``fire_now`` lands the marker is in ``running`` (not ``starting``).
    """
    loop = _SlowLoop(hold_s=1.5)
    registry, _ = _build_registry_and_scheduler(
        (loop, _meta("slow_loop", lock_acquire_timeout_s=0.2))
    )

    first = asyncio.create_task(
        registry.fire_now("slow_loop", triggered_by="user:first")
    )
    # The slow loop's tick body has begun → audit row opened →
    # _note_holder() ran → state='running'.
    await loop.started.wait()

    with pytest.raises(AlreadyRunning) as exc_info:
        await registry.fire_now("slow_loop", triggered_by="user:second")
    assert exc_info.value.lock_holder_state == "running"
    assert exc_info.value.job_run_id is not None
    assert exc_info.value.lock_acquired_at is not None

    await first


@pytest.mark.asyncio
async def test_fire_now_409_lock_holder_state_unknown(engine: None) -> None:
    """``lock_holder_state='unknown'`` — the lock is held (asyncio.Lock
    is locked) but the in-memory holder map has no entry for this job.
    Reproduced by manually acquiring the per-job lock outside the
    registry's fire_now path so no ``_LockHolder`` is staged. The
    fire_now's wait_for(lock.acquire(), timeout=...) then times out
    and the route handler sees ``state='unknown'`` + ``job_run_id=None``.
    """
    loop = _OkLoop()
    registry, _ = _build_registry_and_scheduler(
        (loop, _meta("ok_loop", lock_acquire_timeout_s=0.1))
    )

    # Acquire the lock directly so no holder marker is staged.
    lock = registry._lock_for("ok_loop")
    await lock.acquire()
    try:
        with pytest.raises(AlreadyRunning) as exc_info:
            await registry.fire_now("ok_loop", triggered_by="user:probe")
        err = exc_info.value
        assert err.lock_holder_state == "unknown"
        assert err.job_run_id is None
        assert err.lock_acquired_at is None
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_fire_now_409_lock_holder_state_starting(engine: None) -> None:
    """``lock_holder_state='starting'`` — the manual path has staged its
    holder marker (``state='starting'``) but ``_open_job_run`` has NOT
    yet completed (so the marker hasn't been flipped to ``running`` and
    has ``job_run_id=None``). Reproduced by manually staging a
    ``_LockHolder`` in ``starting`` state while holding the lock.
    """
    from argosy.services.jobs.registry import _LockHolder

    loop = _OkLoop()
    registry, _ = _build_registry_and_scheduler(
        (loop, _meta("ok_loop", lock_acquire_timeout_s=0.1))
    )

    lock = registry._lock_for("ok_loop")
    await lock.acquire()
    try:
        # Stage a starting-state holder marker, mirroring what fire_now
        # does between lock-acquire and _open_job_run committing.
        registry._lock_holders["ok_loop"] = _LockHolder(
            job_run_id=None,
            state="starting",
            acquired_at=datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc),
        )

        with pytest.raises(AlreadyRunning) as exc_info:
            await registry.fire_now("ok_loop", triggered_by="user:second")
        err = exc_info.value
        assert err.lock_holder_state == "starting"
        assert err.job_run_id is None
        assert err.lock_acquired_at is not None
    finally:
        registry._lock_holders.pop("ok_loop", None)
        lock.release()


@pytest.mark.asyncio
async def test_supervisor_exit_intent_clean_does_not_auto_restart(
    engine: None,
) -> None:
    """Spec §3 IMPORTANT #3 — ``exit_intent='clean'`` does NOT auto-restart
    in v1. Operator reconnects manually via the Run-now path.

    Verified by counting :meth:`LongRunningJob.run` invocations after
    a clean exit: it MUST be called exactly once.
    """
    from argosy.orchestrator.loops.base import LongRunningJob

    class _CleanExitJob(LongRunningJob):
        name = "clean_exit_lr"

        def __init__(self) -> None:
            self._exit_intent = "unset"
            self._status = "stopped"
            self.run_calls = 0

        def connection_status(self):  # type: ignore[override]
            return self._status

        async def run(self) -> None:
            self.run_calls += 1
            self._status = "connected"
            self._exit_intent = "clean"
            return

    job = _CleanExitJob()
    reg = JobRegistry()
    reg.register(
        job=job, metadata=_meta("clean_exit_lr", long_running=True)
    )
    await reg.start_supervisors()

    # Wait for the supervisor task to finish — clean exit means it
    # returns rather than looping.
    task = reg._supervisor_tasks.get("clean_exit_lr")
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:  # pragma: no cover - regression guard
            pytest.fail(
                "supervisor did not exit after clean run; v1 should NOT "
                "auto-restart on exit_intent='clean'"
            )

    # Give the event loop a moment to settle the supervisor's exit path.
    await asyncio.sleep(0.05)
    assert job.run_calls == 1, (
        f"clean exit should not auto-restart; run() was called "
        f"{job.run_calls} times"
    )
    # Supervisor is gone from the tracking dict (it pops itself on
    # clean exit per registry.py:1093-1096).
    assert "clean_exit_lr" not in reg._supervisor_tasks

    await reg.stop_supervisors()


@pytest.mark.asyncio
async def test_supervisor_backoff_exponential_growth(engine: None) -> None:
    """Spec §3 — supervisor backoff doubles per crash, capped at 60s.

    Sequence after consecutive crashes: 1s, 2s, 4s, 8s, ... up to 60s.
    Verified by overriding ``_sleep`` with a recorder (no wall-time
    sleep happens) so we can observe the delay sequence the supervisor
    requests.
    """
    from argosy.orchestrator.loops.base import LongRunningJob

    class _CrashingJob(LongRunningJob):
        name = "crashing_lr"

        def __init__(self, *, max_crashes: int) -> None:
            self._exit_intent = "unset"
            self._status = "stopped"
            self.crash_count = 0
            self._max_crashes = max_crashes

        def connection_status(self):  # type: ignore[override]
            return self._status

        async def run(self) -> None:
            self.crash_count += 1
            if self.crash_count >= self._max_crashes:
                # Final cycle: clean exit so the supervisor stops looping.
                self._exit_intent = "clean"
                return
            raise RuntimeError(f"crash #{self.crash_count}")

    job = _CrashingJob(max_crashes=5)
    reg = JobRegistry()
    reg.register(job=job, metadata=_meta("crashing_lr", long_running=True))

    # Capture the supervisor's requested backoff delays — no actual
    # wall-time sleep.
    delays: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        delays.append(seconds)
        return None

    reg._sleep = _record_sleep  # type: ignore[assignment]

    await reg.start_supervisors()

    # Wait until the supervisor exits (final cycle was a clean exit).
    task = reg._supervisor_tasks.get("crashing_lr")
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:  # pragma: no cover - regression guard
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            pytest.fail("supervisor did not exit; delays so far: %r" % delays)

    # 5 cycles: crashes 1-4 + clean exit on #5. After each of cycles
    # 1-4 (crash), the supervisor sleeps; the final cycle exits without
    # sleeping. So we expect 4 delay values: 1s, 2s, 4s, 8s.
    assert len(delays) == 4, (
        f"expected 4 backoff sleeps after 4 crashes; got {delays!r}"
    )
    assert delays[0] == pytest.approx(1.0)
    assert delays[1] == pytest.approx(2.0)
    assert delays[2] == pytest.approx(4.0)
    assert delays[3] == pytest.approx(8.0)
    # Each delay is strictly larger than its predecessor (exp growth).
    for prev, cur in zip(delays, delays[1:]):
        assert cur > prev
        assert cur == pytest.approx(prev * 2.0)


@pytest.mark.asyncio
async def test_supervisor_backoff_capped_at_60s(engine: None) -> None:
    """Once exponential backoff would exceed the 60s cap, every
    subsequent delay STAYS at 60s. Verifies the spec §3 ceiling that
    bounds runaway memory growth for a job crashing on every restart.
    """
    from argosy.orchestrator.loops.base import LongRunningJob

    class _AlwaysCrashJob(LongRunningJob):
        name = "always_crash_lr"

        def __init__(self, *, n_cycles_before_stop: int) -> None:
            self._exit_intent = "unset"
            self._status = "stopped"
            self.crash_count = 0
            self._n = n_cycles_before_stop

        def connection_status(self):  # type: ignore[override]
            return self._status

        async def run(self) -> None:
            self.crash_count += 1
            if self.crash_count >= self._n:
                # Operator-stop to break out of the test loop cleanly.
                self._exit_intent = "clean"
                return
            raise RuntimeError("always crash")

    # 10 cycles: 9 crashes + clean exit. Backoff sequence:
    #   1, 2, 4, 8, 16, 32, 60, 60, 60 → 9 entries, last 3 at the cap.
    job = _AlwaysCrashJob(n_cycles_before_stop=10)
    reg = JobRegistry()
    reg.register(
        job=job, metadata=_meta("always_crash_lr", long_running=True)
    )

    delays: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        delays.append(seconds)

    reg._sleep = _record_sleep  # type: ignore[assignment]
    await reg.start_supervisors()

    task = reg._supervisor_tasks.get("always_crash_lr")
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:  # pragma: no cover
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # 9 delays expected (one per crash).
    assert len(delays) == 9, f"got {delays!r}"
    # Tail of the sequence is the cap.
    assert delays[-1] == pytest.approx(60.0)
    assert delays[-2] == pytest.approx(60.0)
    assert delays[-3] == pytest.approx(60.0)
    # Nothing in the sequence ever exceeds the cap.
    assert all(d <= 60.0 + 1e-9 for d in delays)
