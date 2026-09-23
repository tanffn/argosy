"""Real SQLite lock/restart tests: no mocked persistence or completion seam."""
from datetime import UTC, datetime, timedelta
import json
import importlib.util
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.orchestrator.loops.job_runs_retention import JobRunsRetentionLoop, reap_prior_process_job_runs
from argosy.services.jobs.completion_journal import CompletionJournal
from argosy.services.jobs.registry import JobMetadata, JobRegistry
from argosy.services.jobs.registered_scheduler import RegisteredScheduler
from argosy.state import db as db_mod
from argosy.state.models import Base, JobRun


@pytest_asyncio.fixture
async def locked_db(tmp_path):
    path = tmp_path / "isolated.db"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    eng = db_mod.init_engine(url)

    @event.listens_for(eng.sync_engine, "connect")
    def short_timeout(connection, _record):
        connection.execute("PRAGMA busy_timeout=25")

    def create_production_job_schema(connection):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        # JobRun ORM metadata omits production CHECK constraints. Execute the
        # actual migration for this table, not a relaxed test-only substitute.
        Base.metadata.create_all(connection, tables=[t for t in Base.metadata.sorted_tables if t.name != "job_runs"])
        spec = importlib.util.spec_from_file_location("job_runs_migration", Path(__file__).parents[1] / "alembic/versions/0048_job_runs.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
    async with eng.begin() as connection:
        await connection.run_sync(create_production_job_schema)
    writer = sqlite3.connect(path, timeout=0.025)
    try:
        yield path, url, writer
    finally:
        writer.close()
        await db_mod.dispose_engine()


async def open_run(registry, name="news_daily", started=None):
    return await registry._open_job_run(
        job_name=name, started_at=started or datetime.now(UTC), manual_trigger=False,
        triggered_by="test",
    )


async def get_run(run_id):
    async with db_mod.get_session() as session:
        return await session.get(JobRun, run_id)


@pytest.mark.asyncio
async def test_wake_backlog_larger_than_one_batch_drains_before_retention(locked_db):
    path, url, writer = locked_db
    registry = JobRegistry()
    ids = [await open_run(registry, name=f"job_{n}") for n in range(25)]
    writer.execute("BEGIN IMMEDIATE")
    for run_id in ids:
        with pytest.raises(OperationalError):
            await registry._close_job_run(run_id, status="ok")
    writer.rollback()
    assert len(list(path.parent.glob("*.job-completions/*.json"))) == 25
    await registry.require_completions_drained()
    assert not list(path.parent.glob("*.job-completions/*.json"))
    assert all([(await get_run(run_id)).status == "ok" for run_id in ids])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ok", "error"])
async def test_real_lock_durable_completion_replayed_before_startup_reap(locked_db, status):
    path, url, writer = locked_db
    registry = JobRegistry()
    run_id = await open_run(registry)
    finished = datetime.now(UTC)
    writer.execute("BEGIN IMMEDIATE")
    with pytest.raises(OperationalError, match="locked"):
        await registry._close_job_run(run_id, status=status, finished_at=finished,
                                      output_summary={"analyzed": 0},
                                      error_message="analysis failed" if status == "error" else None)
    assert (await get_run(run_id)).status == "running"
    assert len(list(path.parent.glob("*.job-completions/*.json"))) == 1
    writer.rollback()
    await db_mod.dispose_engine()
    db_mod.init_engine(url)  # New registry AND new engine; no memory recovery.
    assert await reap_prior_process_job_runs() == 0
    row = await get_run(run_id)
    assert row.status == status
    assert row.finished_at.replace(tzinfo=UTC) == finished
    assert json.loads(row.output_summary) == {"analyzed": 0}
    assert not list(path.parent.glob("*.job-completions/*.json"))
    assert await JobRegistry().recover_completions() == 0


@pytest.mark.asyncio
async def test_duplicate_commit_ack_recovers_without_overwrite(locked_db):
    _, _, writer = locked_db
    registry = JobRegistry()
    run_id = await open_run(registry)
    writer.execute("BEGIN IMMEDIATE")
    with pytest.raises(OperationalError):
        await registry._close_job_run(run_id, status="ok", output_summary={"analyzed": 3})
    writer.rollback()
    async with db_mod.get_session() as session:
        journal = CompletionJournal(session.bind.url)
        receipt = next(journal.pending())
        await registry._apply_completion(session, receipt)
        # Simulate crash AFTER DB commit but BEFORE removing receipt.
    assert await JobRegistry().recover_completions() == 1
    assert (await get_run(run_id)).status == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["cancel", "reused_id"])
async def test_replay_preserves_conflicting_terminal_or_identity(locked_db, mutation):
    path, _, writer = locked_db
    registry = JobRegistry()
    run_id = await open_run(registry)
    writer.execute("BEGIN IMMEDIATE")
    with pytest.raises(OperationalError):
        await registry._close_job_run(run_id, status="ok")
    writer.rollback()
    async with db_mod.get_session() as session:
        changes = {"status": "cancelled", "error_message": "operator cancelled"} if mutation == "cancel" else {"idempotency_key": "another-run"}
        await session.execute(update(JobRun).where(JobRun.id == run_id).values(**changes))
        await session.commit()
    with pytest.raises(RuntimeError, match="pending completion"):
        await reap_prior_process_job_runs()
    row = await get_run(run_id)
    assert row.status == ("cancelled" if mutation == "cancel" else "running")
    assert len(list(path.parent.glob("*.job-completions/*.json"))) == 1


class LockingLoop(CadenceLoop):
    name = "news_daily"

    def __init__(self, writer, fail=False):
        due = datetime.now(UTC) - timedelta(hours=2)
        super().__init__(enabled=True, schedule=LoopSchedule(cron=f"{due.minute} {due.hour} * * *", timezone="UTC"))
        self.writer, self.fail, self.calls = writer, fail, 0

    async def tick(self, *, now=None):
        self.calls += 1
        if self.calls == 1:
            self.writer.execute("BEGIN IMMEDIATE")
            if self.fail:
                raise RuntimeError("analysis could not write due to lock")
        return {"analyzed": 0, "failure_count": 0}


def wire(loop, now):
    registry = JobRegistry()
    scheduler = RegisteredScheduler(registry=registry, settings=SimpleNamespace(), clock=lambda: now)
    registry.bind_scheduler(scheduler)
    scheduler.register_loop(loop)
    registry.register(job=loop, metadata=JobMetadata(
        name=loop.name, schedule_cron=loop.schedule.cron, schedule_human="daily",
        source_kind="monitor", description="real lock regression", long_running=False,
    ))
    return scheduler, registry


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_real_scheduler_repairs_status_then_retries_only_failed_analysis(locked_db, fail, monkeypatch):
    _, _, writer = locked_db
    now = datetime.now(UTC) - timedelta(minutes=40)
    loop = LockingLoop(writer, fail)
    scheduler, registry = wire(loop, now)
    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return scheduler.clock()
    monkeypatch.setattr("argosy.services.jobs.registry.datetime", ClockDateTime)
    await scheduler._fire_once(loop)
    assert loop.calls == 1
    writer.rollback()
    # No cadence_state write succeeded either. Durable job receipt still
    # suppresses catch-up while recovery repairs completion separately.
    assert await scheduler._tick_recorded_since(loop.name, now - timedelta(minutes=1))
    scheduler.clock = lambda: datetime.now(UTC)
    await scheduler._recover_failed_jobs()
    assert loop.calls == (2 if fail else 1)
    async with db_mod.get_session() as session:
        rows = list((await session.execute(select(JobRun).order_by(JobRun.id))).scalars())
    assert rows[0].status == ("error" if fail else "ok")
    if fail:
        assert rows[-1].triggered_by == "recovery"
        assert rows[-1].status == "ok"


def test_journal_is_bound_to_actual_database_and_refuses_wrong_database(tmp_path):
    from sqlalchemy.engine import make_url
    first = CompletionJournal(make_url(f"sqlite:///{tmp_path / 'one.db'}"))
    second = CompletionJournal(make_url(f"sqlite:///{tmp_path / 'two.db'}"))
    receipt = dict(run_id=1, idempotency_key="unique", database=first.database,
                   job_name="test", started_at="2026-09-14T00:00:00+00:00",
                   finished_at="2026-09-14T00:00:01+00:00", status="ok",
                   output_summary=None, error_message=None, skip_reason=None)
    first.stage(receipt)
    assert list(second.pending()) == []
    second.stage(receipt)
    with pytest.raises(ValueError, match="identity mismatch"):
        list(second.pending())


@pytest.mark.asyncio
async def test_idempotent_open_different_microseconds_uses_stored_identity(locked_db):
    registry = JobRegistry()
    started = datetime.now(UTC).replace(microsecond=100000) - timedelta(seconds=1)
    first = await open_run(registry, started=started)
    second = await open_run(registry, started=started.replace(microsecond=200000))
    assert first == second
    await registry._close_job_run(second, status="ok")
    assert (await get_run(first)).status == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [False, True, "malformed_field", "invalid_summary_json", "nonfinite_json", "overflow_id"])
async def test_bad_receipt_does_not_block_other_receipts_or_job_recovery(locked_db, corrupt, monkeypatch):
    path, _, writer = locked_db
    registry = JobRegistry()
    bad_id = await open_run(registry, name="bad_job")
    writer.execute("BEGIN IMMEDIATE")
    with pytest.raises(OperationalError):
        await registry._close_job_run(bad_id, status="ok")
    writer.rollback()
    if corrupt:
        target = next(path.parent.glob("*.job-completions/*.json"))
        if corrupt == "overflow_id":
            malformed = json.loads(target.read_text(encoding="utf-8"))
            malformed["run_id"] = "OVERFLOW_ID"
            target.write_text(json.dumps(malformed).replace('"OVERFLOW_ID"', '1e309'), encoding="utf-8")
        elif corrupt in ("malformed_field", "invalid_summary_json", "nonfinite_json"):
            malformed = json.loads(target.read_text(encoding="utf-8"))
            malformed["output_summary"] = {
                "malformed_field": {}, "invalid_summary_json": "invalid json",
                "nonfinite_json": '{"value": NaN}',
            }[corrupt]
            target.write_text(json.dumps(malformed), encoding="utf-8")
        else:
            target.write_text("invalid json", encoding="utf-8")
    else:
        async with db_mod.get_session() as session:
            await session.execute(update(JobRun).where(JobRun.id == bad_id).values(status="cancelled"))
            await session.commit()
    # An unrelated failed analysis has its own pending completion receipt.
    now = datetime.now(UTC) - timedelta(minutes=40)
    loop = LockingLoop(writer, fail=True)
    scheduler, _ = wire(loop, now)
    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return scheduler.clock()
    monkeypatch.setattr("argosy.services.jobs.registry.datetime", ClockDateTime)
    await scheduler._fire_once(loop)
    writer.rollback()
    scheduler.clock = lambda: datetime.now(UTC)
    await scheduler._recover_failed_jobs()
    assert loop.calls == 2
    assert (await get_run(bad_id)).status == ("running" if corrupt else "cancelled")
    with pytest.raises((ValueError, RuntimeError)):
        await reap_prior_process_job_runs()


@pytest.mark.asyncio
async def test_daily_retention_cannot_reap_a_live_job_finishing_concurrently(locked_db):
    import asyncio
    registry = JobRegistry()
    run_id = await open_run(registry, started=datetime.now(UTC) - timedelta(days=2))
    retention_has_writer = asyncio.Event()
    # Instrument only the retention session between real DELETE and UPDATE;
    # completion still uses a real second DB connection and the real journal.
    eng = db_mod.get_engine()
    completion_task = None

    @event.listens_for(eng.sync_engine, "after_cursor_execute")
    def after_delete(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal completion_task
        if statement.startswith("DELETE FROM job_runs"):
            retention_has_writer.set()
            completion_task = asyncio.create_task(registry._close_job_run(run_id, status="ok"))

    try:
        summary = await JobRunsRetentionLoop().tick()
        assert retention_has_writer.is_set()
        assert summary["reaped_running_count"] == 0
        assert completion_task is not None
        try:
            await completion_task
        except OperationalError:
            await registry.recover_completions()
        assert (await get_run(run_id)).status == "ok"
    finally:
        event.remove(eng.sync_engine, "after_cursor_execute", after_delete)


@pytest.mark.parametrize("field,value", [
    ("run_id", True), ("run_id", 2**65), ("job_name", {}),
    ("error_message", []), ("skip_reason", 7), ("status", {}),
    ("output_summary", {}), ("finished_at", 0), ("started_at", None),
])
def test_receipt_rejects_non_sql_scalar_fields(field, value):
    from argosy.services.jobs.completion_journal import validate_receipt
    receipt = dict(run_id=1, idempotency_key="key", job_name="test", database=None,
                   started_at="2026-09-14T00:00:00+00:00", finished_at="2026-09-14T00:00:01+00:00",
                   status="ok", error_message=None, skip_reason=None, output_summary="{}")
    receipt[field] = value
    with pytest.raises(ValueError):
        validate_receipt(receipt)


@pytest.mark.asyncio
async def test_fixture_enforces_actual_production_json_constraint(locked_db):
    run_id = await open_run(JobRegistry())
    async with db_mod.get_session() as session:
        with pytest.raises(IntegrityError, match="ck_job_runs_output_summary_json"):
            await session.execute(update(JobRun).where(JobRun.id == run_id).values(output_summary="invalid json"))
