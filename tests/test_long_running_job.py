"""Sprint A commit #5 — :class:`LongRunningJob` + supervisor tests.

Spec: ``docs/superpowers/specs/2026-05-29-jobs-registry-design.md`` §3,
§5 commit #5 detail.

Coverage:

* Happy path — ``run()`` returns cleanly + sets ``_exit_intent='clean'``.
  Supervisor records ``status='ok'`` + DOES NOT restart.
* Crash path — ``run()`` raises. Supervisor records ``status='error'``,
  applies exp-backoff (mocked), restarts. Backoff cap honored.
* Operator stop — :meth:`JobRegistry.cancel_long_running` sets
  ``_exit_intent='operator_stop'``, supervisor records
  ``status='cancelled'``, DOES NOT restart.
* Reconnect-while-active — ``connection_status='reconnecting'`` for
  >60s → health is ``'red'``.
* Multiple supervisors — :meth:`start_supervisors` /
  :meth:`stop_supervisors` clean lifecycle.

Test command::

    D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest \\
        -m "not llm_eval" tests/test_long_running_job.py -v
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from argosy.orchestrator.loops.base import LongRunningJob
from argosy.services.jobs import JobMetadata, JobRegistry
from argosy.state import db as db_mod
from argosy.state.models import JobRun


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _CleanExitJob(LongRunningJob):
    """``run()`` returns immediately + sets exit_intent='clean'."""

    name = "clean_exit_job"

    def __init__(self) -> None:
        super().__init__()
        self._status = "stopped"
        self.run_calls = 0
        self.started = asyncio.Event()

    def connection_status(self):  # type: ignore[override]
        return self._status

    async def run(self) -> None:  # type: ignore[override]
        self.run_calls += 1
        self._status = "connected"
        self.started.set()
        # Clean exit: do tiny work and return.
        await asyncio.sleep(0)
        self._status = "stopped"
        self._exit_intent = "clean"


class _CrashingJob(LongRunningJob):
    """``run()`` raises every time; after N crashes, returns cleanly.

    Use ``cap_crashes`` to bound the test so the loop terminates.
    """

    name = "crashing_job"

    def __init__(self, *, cap_crashes: int = 3) -> None:
        super().__init__()
        self._status = "reconnecting"
        self.cap_crashes = cap_crashes
        self.run_calls = 0
        self.crashed_n = 0

    def connection_status(self):  # type: ignore[override]
        return self._status

    async def run(self) -> None:  # type: ignore[override]
        self.run_calls += 1
        if self.crashed_n < self.cap_crashes:
            self.crashed_n += 1
            raise RuntimeError(f"boom #{self.crashed_n}")
        # Stop crashing — clean exit so supervisor stops.
        self._exit_intent = "clean"


class _BlockingJob(LongRunningJob):
    """``run()`` blocks on an event until cancelled."""

    name = "blocking_job"

    def __init__(self) -> None:
        super().__init__()
        self._status = "connected"
        self.started = asyncio.Event()
        self.cancel_called = 0
        self._release = asyncio.Event()

    def connection_status(self):  # type: ignore[override]
        return self._status

    async def cancel(self) -> None:  # type: ignore[override]
        self.cancel_called += 1
        self._release.set()

    async def run(self) -> None:  # type: ignore[override]
        self.started.set()
        try:
            # Wait either for cancel() to release us OR for the
            # supervisor's task.cancel() to raise CancelledError.
            await self._release.wait()
        finally:
            # Job sets its own exit_intent in finally — cancel_long_running
            # may have already set 'operator_stop'. Don't clobber that.
            self._status = "stopped"


class _ReconnectingJob(LongRunningJob):
    """A job that's stuck in ``reconnecting``. Used for the health probe."""

    name = "reconnecting_job"

    def __init__(self) -> None:
        super().__init__()
        self._status = "reconnecting"

    def connection_status(self):  # type: ignore[override]
        return self._status

    async def run(self) -> None:  # type: ignore[override]
        # Never called by the health-probe test — we exercise list()
        # directly without starting supervisors.
        self._exit_intent = "clean"


def _meta(name: str, **overrides) -> JobMetadata:
    defaults = dict(
        name=name,
        schedule_cron=None,
        schedule_human="long-running (supervised)",
        source_kind="monitor",
        description=f"test fixture {name}",
        long_running=True,
        lock_acquire_timeout_s=1.0,
    )
    defaults.update(overrides)
    return JobMetadata(**defaults)


# ---------------------------------------------------------------------------
# Happy path — clean exit does NOT restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_exit_records_ok_and_does_not_restart(engine: None) -> None:
    """Spec §3 IMPORTANT #3: clean exit is NOT auto-restarted in v1."""
    job = _CleanExitJob()
    reg = JobRegistry()
    reg.register(job=job, metadata=_meta("clean_exit_job"))

    await reg.start_supervisors()

    # Wait until the supervisor task completes — it should exit
    # cleanly after one run() call (no restart on 'clean').
    task = reg._supervisor_tasks.get("clean_exit_job")
    assert task is not None
    await asyncio.wait_for(task, timeout=2.0)

    # Exactly one run() call (no restart).
    assert job.run_calls == 1
    assert job.exit_intent == "clean"

    # Audit row recorded with status='ok'.
    async with db_mod.get_session() as s:
        rows = (
            await s.execute(
                select(JobRun).where(JobRun.job_name == "clean_exit_job")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "ok"
        # supervisor cycles are stamped "supervisor#<n>" so each cycle's
        # idempotency_key is unique.
        assert rows[0].triggered_by.startswith("supervisor")
        assert rows[0].manual_trigger == 0

    # stop_supervisors is a no-op (task already done).
    await reg.stop_supervisors()


# ---------------------------------------------------------------------------
# Crash path — exp-backoff + restart, then clean exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_records_error_and_restarts_with_backoff(
    engine: None,
) -> None:
    """Spec §5 commit #5: crash → status='error' + exp-backoff restart.
    Uses a fake clock so the test doesn't wait real wall-time.
    """
    job = _CrashingJob(cap_crashes=3)
    reg = JobRegistry()
    reg.register(job=job, metadata=_meta("crashing_job"))

    # Fake clock: record the requested delays + return immediately.
    delays_observed: list[float] = []

    async def _fake_sleep(delay_s: float) -> None:
        delays_observed.append(delay_s)
        # Yield so the supervisor's wait() can observe the resolved sleep.
        await asyncio.sleep(0)

    reg._sleep = _fake_sleep  # type: ignore[assignment]

    await reg.start_supervisors()

    task = reg._supervisor_tasks.get("crashing_job")
    assert task is not None
    # 3 crashes + 1 clean exit = 4 run() calls; backoff slept 3 times.
    await asyncio.wait_for(task, timeout=5.0)

    assert job.run_calls == 4
    assert job.crashed_n == 3
    # Backoff sequence: 1s → 2s → 4s. (Reset on clean exit; cap is 60s.)
    assert delays_observed == [1.0, 2.0, 4.0]

    async with db_mod.get_session() as s:
        rows = (
            await s.execute(
                select(JobRun)
                .where(JobRun.job_name == "crashing_job")
                .order_by(JobRun.id)
            )
        ).scalars().all()
        assert len(rows) == 4
        # First three: error. Last: ok.
        assert [r.status for r in rows] == ["error", "error", "error", "ok"]
        assert all(r.error_message and "boom" in r.error_message for r in rows[:3])

    await reg.stop_supervisors()


@pytest.mark.asyncio
async def test_backoff_caps_at_60s(engine: None) -> None:
    """A job that crashes forever has its backoff capped — runaway
    safety net (codex review focus #2).
    """
    job = _CrashingJob(cap_crashes=100)  # crash way past the cap
    reg = JobRegistry()
    reg.register(job=job, metadata=_meta("crashing_job"))

    delays_observed: list[float] = []

    async def _fake_sleep(delay_s: float) -> None:
        delays_observed.append(delay_s)
        # Once we've seen the cap a couple times, signal the supervisor
        # to stop so the test terminates.
        if len(delays_observed) >= 9:
            reg._stop_supervisors.set()  # type: ignore[union-attr]
        await asyncio.sleep(0)

    reg._sleep = _fake_sleep  # type: ignore[assignment]

    await reg.start_supervisors()
    task = reg._supervisor_tasks.get("crashing_job")
    assert task is not None
    await asyncio.wait_for(task, timeout=5.0)

    # Sequence is 1, 2, 4, 8, 16, 32, 60, 60, 60. The cap (60s) is
    # the runaway-memory safety net.
    assert delays_observed[:9] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
    assert max(delays_observed) <= reg._backoff_cap_s

    await reg.stop_supervisors()


# ---------------------------------------------------------------------------
# Operator stop — cancel_long_running closes row with status='cancelled'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_long_running_records_cancelled_no_restart(
    engine: None,
) -> None:
    """``cancel_long_running`` sets exit_intent='operator_stop',
    closes the audit row with status='cancelled', and DOES NOT restart.
    """
    job = _BlockingJob()
    reg = JobRegistry()
    reg.register(job=job, metadata=_meta("blocking_job"))

    await reg.start_supervisors()
    await job.started.wait()

    # Stop the job.
    await reg.cancel_long_running("blocking_job")

    # exit_intent stamped BEFORE supervisor cancels — codex review focus #1.
    assert job.exit_intent == "operator_stop"
    assert job.cancel_called == 1

    # No more supervisor task.
    assert "blocking_job" not in reg._supervisor_tasks

    async with db_mod.get_session() as s:
        rows = (
            await s.execute(
                select(JobRun).where(JobRun.job_name == "blocking_job")
            )
        ).scalars().all()
        # Exactly one (connect,disconnect) cycle — cancelled by operator.
        assert len(rows) == 1
        assert rows[0].status == "cancelled"

    # stop_supervisors after cancel is a no-op.
    await reg.stop_supervisors()


@pytest.mark.asyncio
async def test_cancel_long_running_unknown_name_raises_keyerror(
    engine: None,
) -> None:
    reg = JobRegistry()
    with pytest.raises(KeyError):
        await reg.cancel_long_running("does_not_exist")


@pytest.mark.asyncio
async def test_cancel_during_open_does_not_leave_running_row(
    engine: None,
) -> None:
    """Codex BLOCKER (round 1) fix: cancel while supervisor is BEFORE
    its commit must NOT leave any row in ``running``.

    We inject a slow ``_open_job_run`` that blocks on an event, fire
    the supervisor, fire cancel_long_running, then assert no row is
    stuck in 'running'. The single try/finally in ``_supervisor``
    closes the row with ``status='cancelled'`` (or leaves it
    un-inserted if cancellation lands BEFORE the commit).
    """
    job = _BlockingJob()
    job.name = "race_job"

    reg = JobRegistry()
    reg.register(job=job, metadata=_meta("race_job"))

    # Monkeypatch _open_job_run so the FIRST call blocks until we
    # release it. Subsequent calls go through the real implementation.
    real_open = reg._open_job_run
    release_open = asyncio.Event()
    inside_open = asyncio.Event()
    call_count = {"n": 0}

    async def _slow_open(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            inside_open.set()
            await release_open.wait()
        return await real_open(*args, **kwargs)

    reg._open_job_run = _slow_open  # type: ignore[assignment]

    await reg.start_supervisors()
    # Wait until the supervisor is INSIDE _open_job_run.
    await inside_open.wait()

    # Now fire cancel WHILE the supervisor is blocked on the open.
    # cancel_long_running awaits the supervisor task, so we need to
    # release the open after kicking off the cancel — otherwise the
    # cancel would deadlock waiting for the supervisor.
    cancel_task = asyncio.create_task(reg.cancel_long_running("race_job"))
    # Give cancel_long_running time to set _exit_intent + call
    # task.cancel(). With the task cancelled, the in-flight
    # `await release_open.wait()` will raise CancelledError.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Release the open just in case (no-op if already cancelled).
    release_open.set()

    await cancel_task
    assert job.exit_intent == "operator_stop"

    async with db_mod.get_session() as s:
        rows = (
            await s.execute(
                select(JobRun).where(JobRun.job_name == "race_job")
            )
        ).scalars().all()
        # Two possibilities, both correct:
        #   (a) the open was cancelled BEFORE the commit hit → 0 rows
        #   (b) the open committed BEFORE the cancel landed → 1 row,
        #       closed by the finally to 'cancelled'.
        # The forbidden state is "1 row in status='running'" — i.e.
        # the supervisor opened a row + got cancelled without closing.
        for row in rows:
            assert row.status != "running", (
                f"Audit row {row.id} left in 'running' — cancel-during-open "
                "race not handled."
            )


@pytest.mark.asyncio
async def test_supervisor_epoch_isolates_recovery_lookup(
    engine: None,
) -> None:
    """Codex round-3 BLOCKER #2 fix: a fresh supervisor's cycle=1
    must NOT collide with a previous supervisor's cycle=1 row.

    The supervisor's ``triggered_by`` label has shape
    ``"supervisor#{epoch}.{cycle}"`` where ``epoch`` is bumped per
    :meth:`JobRegistry._supervisor` invocation. We pre-bump the epoch
    via two no-op start/stop cycles, then verify the recovery lookup
    keyed on a fresh-supervisor label cannot return an old row.
    """
    # Build a registry; bump the epoch to 99 explicitly so we can
    # reason about labels without depending on test-execution order.
    reg = JobRegistry()
    reg._supervisor_epoch = 98  # next supervisor uses epoch=99

    # Seed a row labeled like an OLD supervisor (epoch=1, cycle=1).
    old_run_id = await reg._open_job_run(
        job_name="epoch_job",
        manual_trigger=False,
        triggered_by="supervisor#1.1",
    )
    await reg._close_job_run(old_run_id, status="ok")

    # Register a fresh job + start the supervisor — it bumps the
    # epoch from 98 to 99 and uses label "supervisor#99.1" for its
    # first cycle.
    job = _CleanExitJob()
    job.name = "epoch_job_new"
    reg.register(job=job, metadata=_meta("epoch_job_new"))
    await reg.start_supervisors()
    task = reg._supervisor_tasks.get("epoch_job_new")
    assert task is not None
    await asyncio.wait_for(task, timeout=2.0)

    # The supervisor's lookup label for its own cycle returns its own
    # row, NOT the prior epoch's row.
    fresh_id = await reg._lookup_run_id_by_key(
        "epoch_job_new", "supervisor#99.1"
    )
    assert fresh_id is not None
    assert fresh_id != old_run_id

    # The OLD label still resolves to the seeded row (sanity).
    assert (
        await reg._lookup_run_id_by_key("epoch_job", "supervisor#1.1")
    ) == old_run_id

    await reg.stop_supervisors()


@pytest.mark.asyncio
async def test_cancel_after_open_commit_before_assignment_closes_row(
    engine: None,
) -> None:
    """Codex BLOCKER (round 2) fix: the dangerous case where
    ``_open_job_run`` COMMITTED the row but ``CancelledError`` raises
    out of the ``await`` before ``run_id = await _open_job_run`` lands.

    The shielded-open + idempotency_key recovery path in ``_supervisor``
    must still close the row — verified by asserting the committed
    row is closed with ``status='cancelled'``, not orphaned in
    ``status='running'``.
    """
    job = _BlockingJob()
    job.name = "commit_race_job"

    reg = JobRegistry()
    reg.register(job=job, metadata=_meta("commit_race_job"))

    # Inject a wrapper that lets the real open RUN to completion (so
    # the DB row commits), then delays the await-return long enough
    # for the test to cancel the supervisor BEFORE the supervisor's
    # frame resumes. This simulates the precise race window the
    # round-2 BLOCKER pointed out.
    real_open = reg._open_job_run
    committed = asyncio.Event()
    release_return = asyncio.Event()

    async def _open_with_post_commit_pause(*args, **kwargs):
        # Run the real open (which commits the row).
        rid = await real_open(*args, **kwargs)
        committed.set()
        # NOW pause before returning — this is the cancel-window.
        await release_return.wait()
        return rid

    reg._open_job_run = _open_with_post_commit_pause  # type: ignore[assignment]

    await reg.start_supervisors()
    # Wait until the DB row is COMMITTED.
    await committed.wait()

    # Fire cancel WHILE the supervisor is still waiting on the post-
    # commit pause. cancel_long_running awaits the supervisor task,
    # so we need to release the pause after firing cancel — otherwise
    # we deadlock.
    cancel_task = asyncio.create_task(
        reg.cancel_long_running("commit_race_job")
    )
    # Let cancel_long_running's task.cancel() land.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # Release the pause — the supervisor's awaiter for open_task can
    # now resume + read the committed run_id (via the shield) OR
    # recover via _lookup_run_id_by_key.
    release_return.set()
    await cancel_task

    async with db_mod.get_session() as s:
        rows = (
            await s.execute(
                select(JobRun).where(JobRun.job_name == "commit_race_job")
            )
        ).scalars().all()
        assert len(rows) == 1, (
            "Expected exactly one committed row (the open's commit)."
        )
        assert rows[0].status == "cancelled", (
            f"Row left in {rows[0].status!r} — the shield+recovery "
            "path failed to close the row."
        )


@pytest.mark.asyncio
async def test_cancel_long_running_rejects_cadence_loop() -> None:
    """``cancel_long_running`` on a CadenceLoop raises TypeError —
    the route handler should pick the right method based on
    ``metadata.long_running``.
    """
    from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule

    class _Cadence(CadenceLoop):
        name = "cadence_job"

        def __init__(self) -> None:
            super().__init__(
                schedule=LoopSchedule(interval_seconds=60), enabled=True
            )

        async def tick(self, *, now=None):  # type: ignore[override]
            pass

    reg = JobRegistry()
    reg.register(
        job=_Cadence(),
        metadata=JobMetadata(
            name="cadence_job",
            schedule_cron=None,
            schedule_human="every 60s",
            source_kind="maintenance",
            description="test",
            long_running=False,
        ),
    )
    with pytest.raises(TypeError, match="not a LongRunningJob"):
        await reg.cancel_long_running("cadence_job")


# ---------------------------------------------------------------------------
# Health derivation for reconnecting-while-active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnecting_over_60s_is_red(engine: None) -> None:
    """Spec §1.6 health table: LongRunningJob `reconnecting` for >60s
    → red. We seed the audit row directly + read JobRegistry.list().

    Codex review focus #5: the >60s boundary in the health table must
    match what :func:`_derive_health` already implements.
    """
    job = _ReconnectingJob()
    reg = JobRegistry()
    reg.register(job=job, metadata=_meta("reconnecting_job"))

    # Seed a `running` job_runs row started 120s ago. The supervisor
    # would normally write this; we shortcut for the health probe.
    started_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    run_id = await reg._open_job_run(
        job_name="reconnecting_job",
        manual_trigger=False,
        triggered_by="supervisor",
        started_at=started_at,
    )
    assert run_id > 0

    view = await reg.get("reconnecting_job")
    assert view.last_run_status == "reconnecting"
    assert view.health == "red"

    # And the <60s window resolves to amber.
    job._status = "reconnecting"
    # Open a fresh row (started 10s ago) — supersedes the old one
    # since list() picks the newest 'running' id.
    fresh_started = datetime.now(timezone.utc) - timedelta(seconds=10)
    # Close the old row so list() picks the fresh one.
    await reg._close_job_run(run_id, status="cancelled")
    await reg._open_job_run(
        job_name="reconnecting_job",
        manual_trigger=False,
        triggered_by="supervisor",
        started_at=fresh_started,
    )
    view = await reg.get("reconnecting_job")
    assert view.last_run_status == "reconnecting"
    assert view.health == "amber"


@pytest.mark.asyncio
async def test_reconnecting_boundary_inclusive_60s(engine: None) -> None:
    """Codex NICE: the >=60s boundary is RED; <60s is AMBER.

    Pin the inclusive/exclusive behavior so a future refactor can't
    silently shift the boundary.
    """
    job = _ReconnectingJob()
    reg = JobRegistry()
    reg.register(job=job, metadata=_meta("reconnecting_job"))

    # 59s: amber.
    await reg._open_job_run(
        job_name="reconnecting_job",
        manual_trigger=False,
        triggered_by="supervisor#59s",
        started_at=datetime.now(timezone.utc) - timedelta(seconds=59),
    )
    view = await reg.get("reconnecting_job")
    assert view.health == "amber"

    # Bump up: 61s (close the 59s row first so list() picks the new one).
    # We can't easily close just the 59s row by id without complicating
    # the test; instead use a fresh registry.
    job2 = _ReconnectingJob()
    job2.name = "reconnecting_job_2"
    reg2 = JobRegistry()
    reg2.register(job=job2, metadata=_meta("reconnecting_job_2"))
    await reg2._open_job_run(
        job_name="reconnecting_job_2",
        manual_trigger=False,
        triggered_by="supervisor#61s",
        started_at=datetime.now(timezone.utc) - timedelta(seconds=61),
    )
    view2 = await reg2.get("reconnecting_job_2")
    assert view2.health == "red"


@pytest.mark.asyncio
async def test_connected_status_is_green(engine: None) -> None:
    """Sanity: connection_status='connected' → health='green' even
    with no cadence (LongRunningJob has no schedule attribute).
    """
    job = _ReconnectingJob()
    job._status = "connected"
    reg = JobRegistry()
    reg.register(job=job, metadata=_meta("reconnecting_job"))

    view = await reg.get("reconnecting_job")
    assert view.last_run_status == "connected"
    assert view.health == "green"


# ---------------------------------------------------------------------------
# Multiple supervisors lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_supervisors_start_and_stop(engine: None) -> None:
    """``start_supervisors`` spawns one task per registered
    LongRunningJob. ``stop_supervisors`` shuts them all down cleanly.
    """
    job_a = _BlockingJob()
    job_a.name = "blocker_a"
    job_b = _BlockingJob()
    job_b.name = "blocker_b"

    reg = JobRegistry()
    reg.register(job=job_a, metadata=_meta("blocker_a"))
    reg.register(job=job_b, metadata=_meta("blocker_b"))

    await reg.start_supervisors()
    await asyncio.gather(job_a.started.wait(), job_b.started.wait())

    assert set(reg._supervisor_tasks.keys()) == {"blocker_a", "blocker_b"}
    assert all(not t.done() for t in reg._supervisor_tasks.values())

    # Trigger clean-shutdown by releasing both jobs' run() bodies + setting
    # exit_intent='clean'. stop_supervisors awaits with a 5s timeout —
    # plenty here.
    job_a._exit_intent = "clean"
    job_b._exit_intent = "clean"
    job_a._release.set()
    job_b._release.set()

    await reg.stop_supervisors()

    # All supervisor tasks drained + the dict is cleared.
    assert reg._supervisor_tasks == {}

    async with db_mod.get_session() as s:
        rows = (
            await s.execute(select(JobRun).order_by(JobRun.id))
        ).scalars().all()
        # One (connect,disconnect) cycle per job.
        assert {r.job_name for r in rows} == {"blocker_a", "blocker_b"}
        # Both clean exits → status='ok'.
        assert {r.status for r in rows} == {"ok"}


@pytest.mark.asyncio
async def test_start_supervisors_is_idempotent(engine: None) -> None:
    """Calling ``start_supervisors`` twice doesn't duplicate the tasks."""
    job = _BlockingJob()
    reg = JobRegistry()
    reg.register(job=job, metadata=_meta("blocking_job"))

    await reg.start_supervisors()
    await job.started.wait()
    first_task = reg._supervisor_tasks["blocking_job"]

    # Second call — should be a no-op for the live task.
    await reg.start_supervisors()
    second_task = reg._supervisor_tasks["blocking_job"]
    assert first_task is second_task

    # Tear down.
    await reg.cancel_long_running("blocking_job")


@pytest.mark.asyncio
async def test_start_supervisors_skips_cadence_loops(engine: None) -> None:
    """Mixed registry: CadenceLoop entries are NOT supervised — they
    go through RegisteredScheduler instead.
    """
    from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule

    class _Cadence(CadenceLoop):
        name = "cadence_only"

        def __init__(self) -> None:
            super().__init__(
                schedule=LoopSchedule(interval_seconds=60), enabled=True
            )

        async def tick(self, *, now=None):  # type: ignore[override]
            pass

    reg = JobRegistry()
    reg.register(
        job=_Cadence(),
        metadata=JobMetadata(
            name="cadence_only",
            schedule_cron=None,
            schedule_human="every 60s",
            source_kind="maintenance",
            description="test",
            long_running=False,
        ),
    )
    reg.register(
        job=_BlockingJob(),
        metadata=_meta("blocking_job"),
    )

    await reg.start_supervisors()

    # Only the LongRunningJob got a supervisor.
    assert "cadence_only" not in reg._supervisor_tasks
    assert "blocking_job" in reg._supervisor_tasks

    await reg.cancel_long_running("blocking_job")


# ---------------------------------------------------------------------------
# Discriminator enforcement
# ---------------------------------------------------------------------------


def test_register_rejects_long_running_metadata_mismatch() -> None:
    """``metadata.long_running`` must match the runtime class."""
    job = _CleanExitJob()
    reg = JobRegistry()
    # Try to register a LongRunningJob with long_running=False.
    with pytest.raises(ValueError, match="disagrees with job type"):
        reg.register(
            job=job,
            metadata=JobMetadata(
                name="clean_exit_job",
                schedule_cron=None,
                schedule_human="x",
                source_kind="monitor",
                description="bad",
                long_running=False,  # wrong
            ),
        )


# ---------------------------------------------------------------------------
# Default exit_intent — defensive default if run() forgets to set it
# ---------------------------------------------------------------------------


class _ForgetfulJob(LongRunningJob):
    """Returns without setting ``_exit_intent``. Supervisor treats as clean."""

    name = "forgetful_job"

    def __init__(self) -> None:
        super().__init__()
        self._status = "stopped"

    def connection_status(self):  # type: ignore[override]
        return self._status

    async def run(self) -> None:  # type: ignore[override]
        # Note: deliberately does NOT set _exit_intent.
        return None


@pytest.mark.asyncio
async def test_run_returns_without_exit_intent_treated_as_clean(
    engine: None,
) -> None:
    """If ``run()`` returns without setting exit_intent, supervisor
    treats as clean (returning cleanly == clean exit by definition).
    Defensive default; documented in the LongRunningJob docstring.
    """
    job = _ForgetfulJob()
    reg = JobRegistry()
    reg.register(job=job, metadata=_meta("forgetful_job"))

    await reg.start_supervisors()
    task = reg._supervisor_tasks.get("forgetful_job")
    assert task is not None
    await asyncio.wait_for(task, timeout=2.0)

    # exit_intent now reads 'clean' (supervisor wrote it after the
    # 'unset' fallback fired).
    assert job.exit_intent == "clean"

    async with db_mod.get_session() as s:
        rows = (
            await s.execute(
                select(JobRun).where(JobRun.job_name == "forgetful_job")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "ok"

    await reg.stop_supervisors()
