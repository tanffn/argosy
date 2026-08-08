"""Fail-loud status-derivation fixes (silent-success / fail-open bug class).

Covers the "make failures visible" layer:

1. The summary→status contract (:mod:`argosy.services.jobs.summary_status`)
   and its wiring into ``RegisteredScheduler`` + base ``Scheduler``:
   a raise-free tick whose ``output_summary`` reports failure now closes
   ``job_runs.status='error'`` / ``cadence_state.last_status='error'``
   instead of a green ``ok``.
2. Anomaly runner: an agent crash renders a distinct ``check_failed``
   state, not "all clear".
3. Watchdog: reads ``job_runs`` for non-ok rows (fail-open jobs).

Run::

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest \
        -m "not llm_eval" -p no:cacheprovider -q tests/test_fail_loud_status.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs import JobMetadata, JobRegistry, RegisteredScheduler
from argosy.services.jobs.summary_status import (
    FAILURE_STATUS,
    OK_STATUS,
    derive_run_status,
    summary_signals_failure,
)
from argosy.state import db as db_mod
from argosy.state.models import CadenceState, JobRun


# ---------------------------------------------------------------------------
# 1. Pure contract unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "summary",
    [
        None,
        {},
        {"counts": {"ticks": 3}},
        {"status": "ok"},
        {"errors": []},  # empty list is the healthy sentinel
        {"adapter_errors": 0},
        {"streams": {"a": {"status": "ok"}, "b": {"status": "ok"}}},
        {"attempted": 5, "succeeded": 5},
        {"attempted": 0, "succeeded": 0},  # nothing attempted → not failure
    ],
)
def test_contract_ok_cases(summary) -> None:
    status, reason = derive_run_status(summary)
    assert status == OK_STATUS
    assert reason is None
    assert summary_signals_failure(summary) is False


@pytest.mark.parametrize(
    "summary",
    [
        {"status": "error"},
        {"status": "failed"},
        {"status": "degraded"},
        {"adapter_errors": 2},
        {"errors": ["boom", "bang"]},
        {"failure_count": 1},
        {"failed_streams": 3},
        {"streams": {"a": {"status": "error"}, "b": {"status": "ok"}}},
        {"streams": {"a": {"status": "error"}, "b": {"status": "error"}}},
        {"items": [{"status": "ok"}, {"status": "failed"}]},
        {"attempted": 4, "succeeded": 0},  # zero-work-done
        {"total": 10, "ok": 0},
    ],
)
def test_contract_failure_cases(summary) -> None:
    status, reason = derive_run_status(summary)
    assert status == FAILURE_STATUS
    assert reason  # non-empty human string
    assert summary_signals_failure(summary) is True


# ---------------------------------------------------------------------------
# Test loops
# ---------------------------------------------------------------------------


class _FailingSummaryLoop(CadenceLoop):
    """Returns WITHOUT raising but its summary reports adapter errors."""

    name = "failing_summary_loop"

    def __init__(self) -> None:
        super().__init__(
            schedule=LoopSchedule(interval_seconds=60), enabled=True
        )
        self.last_output_summary = None

    async def tick(self, *, now=None):  # type: ignore[override]
        summary = {"adapter_errors": 2, "processed": 0}
        self.last_output_summary = summary
        return summary


class _AllStreamsFailedLoop(CadenceLoop):
    name = "all_streams_failed_loop"

    def __init__(self) -> None:
        super().__init__(
            schedule=LoopSchedule(interval_seconds=60), enabled=True
        )
        self.last_output_summary = None

    async def tick(self, *, now=None):  # type: ignore[override]
        summary = {
            "streams": {
                "gov": {"status": "error", "error": "x"},
                "insider": {"status": "error", "error": "y"},
            },
            "streams_total": 2,
            "streams_failed": 2,
        }
        self.last_output_summary = summary
        return summary


class _CleanLoop(CadenceLoop):
    name = "clean_loop"

    def __init__(self) -> None:
        super().__init__(
            schedule=LoopSchedule(interval_seconds=60), enabled=True
        )
        self.last_output_summary = None

    async def tick(self, *, now=None):  # type: ignore[override]
        summary = {"counts": {"ok": 3}, "errors": []}
        self.last_output_summary = summary
        return summary


class _DummySettings:
    pass


def _meta(name: str) -> JobMetadata:
    return JobMetadata(
        name=name,
        schedule_cron=None,
        schedule_human="every 60s",
        source_kind="maintenance",
        description=f"test fixture {name}",
        long_running=False,
        lock_acquire_timeout_s=1.0,
    )


def _build(*loops):
    registry = JobRegistry()
    scheduler = RegisteredScheduler(
        user_id="ariel", settings=_DummySettings(), registry=registry
    )
    registry.bind_scheduler(scheduler)
    for loop in loops:
        scheduler.register_loop(loop)
        registry.register(job=loop, metadata=_meta(loop.name))
    return registry, scheduler


# ---------------------------------------------------------------------------
# 1a. RegisteredScheduler close-status derivation (KEYSTONE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failing_summary_closes_non_ok(engine: None) -> None:
    """A raise-free tick reporting adapter_errors closes status='error'."""
    loop = _FailingSummaryLoop()
    registry, _ = _build(loop)

    run_id = await registry.fire_now(loop.name, triggered_by="user:test")

    async with db_mod.get_session() as s:
        row = (
            await s.execute(select(JobRun).where(JobRun.id == run_id))
        ).scalar_one()
        assert row.status == "error", "fail-open: summary reported failure"
        assert row.error_message and "adapter_errors" in row.error_message
        # output_summary is still persisted for forensics.
        assert row.output_summary is not None

        cad = (
            await s.execute(
                select(CadenceState).where(CadenceState.loop_name == loop.name)
            )
        ).scalar_one()
        assert cad.last_status == "error"


@pytest.mark.asyncio
async def test_all_streams_failed_closes_non_ok(engine: None) -> None:
    loop = _AllStreamsFailedLoop()
    registry, _ = _build(loop)

    run_id = await registry.fire_now(loop.name, triggered_by="user:test")

    async with db_mod.get_session() as s:
        row = (
            await s.execute(select(JobRun).where(JobRun.id == run_id))
        ).scalar_one()
        assert row.status == "error"


@pytest.mark.asyncio
async def test_clean_summary_still_ok(engine: None) -> None:
    """Regression guard: a genuinely clean summary keeps status='ok'."""
    loop = _CleanLoop()
    registry, _ = _build(loop)

    run_id = await registry.fire_now(loop.name, triggered_by="user:test")

    async with db_mod.get_session() as s:
        row = (
            await s.execute(select(JobRun).where(JobRun.id == run_id))
        ).scalar_one()
        assert row.status == "ok"
        assert row.error_message is None


# ---------------------------------------------------------------------------
# 1b. Base Scheduler cadence_state derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_scheduler_records_error_on_failure_summary(
    engine: None,
) -> None:
    from argosy.orchestrator.scheduler import Scheduler

    loop = _FailingSummaryLoop()
    scheduler = Scheduler(user_id="ariel", settings=_DummySettings())
    scheduler.register_loop(loop)

    await scheduler._fire_once(loop, force=True)

    async with db_mod.get_session() as s:
        cad = (
            await s.execute(
                select(CadenceState).where(CadenceState.loop_name == loop.name)
            )
        ).scalar_one()
        assert cad.last_status == "error"
        assert cad.last_error and "adapter_errors" in cad.last_error


# ---------------------------------------------------------------------------
# 2. Anomaly runner — a crashed check renders a distinct "check_failed"
#    state, NOT green.
# ---------------------------------------------------------------------------


class _RaisingAnomalyAgent:
    def run_sync(self, **kwargs):  # noqa: ANN003
        raise RuntimeError("anomaly agent exploded")


def _anomaly_sync_session(tmp_path):
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import Base, User

    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'anomaly.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.add(User(id="ariel", plan="free"))
    db.commit()
    return db, engine


def test_anomaly_agent_crash_renders_check_failed(tmp_path, monkeypatch) -> None:
    """The anomaly agent CRASHING must NOT persist an all-clear row.

    Before the fix, an exception in ``agent.run_sync`` was swallowed into
    an empty report with an all-zero severity summary → the home banner
    showed "all clear" for a check that never ran. The fix stamps a
    distinct ``check_failed`` marker.
    """
    from decimal import Decimal
    from datetime import date

    from argosy.services.anomaly_runner import run_anomaly_check
    from argosy.state.models import (
        AnomalyReport,
        ExpenseSource,
        ExpenseStatement,
        ExpenseTransaction,
        UserFile,
    )

    db, engine = _anomaly_sync_session(tmp_path)
    try:
        # Seed a Card 2923 statement so a watchlist entry is applicable
        # (otherwise the runner short-circuits before the agent call).
        f = UserFile(
            user_id="ariel", sha256="a" * 64,
            original_name="d.html", sanitized_name="d.html",
            mime_type="text/html", kind="other", size_bytes=1,
            storage_path="/tmp/d", source="expense_statement",
        )
        db.add(f)
        db.flush()
        src = ExpenseSource(
            user_id="ariel", kind="card", issuer="discount",
            external_id="2923", display_name="Discount Card 2923",
        )
        db.add(src)
        db.flush()
        stmt = ExpenseStatement(
            user_id="ariel", source_id=src.id, file_id=f.id,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 28),
            parsed_total_nis=Decimal("100"), declared_total_nis=Decimal("100"),
            parser_name="discount", parser_version="0.1.0", status="parsed",
        )
        db.add(stmt)
        db.flush()
        db.add(ExpenseTransaction(
            user_id="ariel", source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 5, 15),
            merchant_raw="fee", merchant_normalized="card fee",
            amount_nis=Decimal("12.50"), direction="debit",
            tx_type="regular", raw_row_json="{}",
        ))
        db.commit()

        monkeypatch.setattr(
            "argosy.api.events.publish_event_threadsafe",
            lambda *a, **k: None,
        )

        row = run_anomaly_check(
            "ariel", db, triggered_by="manual",
            source_statement_id=stmt.id, agent=_RaisingAnomalyAgent(),
        )
        assert isinstance(row, AnomalyReport)
        sev = json.loads(row.severity_summary_json)
        # Still no RED/AMBER/YELLOW (nothing ran) BUT a distinct failed flag.
        assert sev.get("check_failed") is True, sev
        assert "error" in sev
        payload = json.loads(row.report_json)
        assert payload.get("check_failed") is True
        assert payload.get("_runner_error")
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 3. Watchdog reads job_runs for non-ok (fail-open) rows.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_flags_failed_job_runs(engine: None) -> None:
    from datetime import timedelta

    from argosy.orchestrator.watchdog import collect_signals
    from argosy.state.models import User

    now = datetime.now(timezone.utc)
    async with db_mod.get_session() as s:
        s.add(User(id="ariel"))
        s.add(
            JobRun(
                job_name="prediction_evaluator",
                started_at=now - timedelta(minutes=5),
                finished_at=now - timedelta(minutes=4),
                status="error",
                error_message="adapter_errors=3",
                idempotency_key="k-fail-1",
            )
        )
        s.add(
            JobRun(
                job_name="news_daily",
                started_at=now - timedelta(minutes=3),
                finished_at=now - timedelta(minutes=2),
                status="ok",
                idempotency_key="k-ok-1",
            )
        )
        await s.commit()

    sig = await collect_signals("ariel")
    assert "prediction_evaluator" in sig.failed_job_runs
    assert "news_daily" not in sig.failed_job_runs
    assert sig.failed_job_run_count == 1
    breach_signals = {b["signal"] for b in sig.breaches}
    assert "job_runs_failed" in breach_signals
