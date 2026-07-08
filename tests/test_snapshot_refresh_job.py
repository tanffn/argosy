"""SnapshotRefreshJob — registration, job_runs wiring, and error paths.

Complements ``tests/test_snapshot_refresh.py`` (service math). Covers:

* the app registers the job ENABLED (Ariel's 2026-07-08 go),
* schedule metadata reads correctly (no stale "manual-only" text),
* a tick failure lands ``job_runs.status='error'`` via the scheduler seam,
* a tick success lands the summary — with ``reprice_miss:*`` symbols
  ENUMERATED — in ``job_runs.output_summary`` for /admin review,
* the critique-reconcile default refresher runs the refresh OFF the event
  loop (regression: ``asyncio.run() cannot be called from a running event
  loop`` made every quote a miss — live incident 2026-07-07 18:00).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re

import pytest
from sqlalchemy import select

from argosy.services.jobs import JobRegistry, RegisteredScheduler
from argosy.services.jobs.snapshot_refresh_job import (
    SnapshotRefreshJob,
    snapshot_refresh_metadata,
)
from argosy.state import db as db_mod
from argosy.state.models import JobRun


class _DummySettings:
    """Scheduler only reads settings.cadences.* in register_default_loops."""


class _NullSession:
    def close(self) -> None:  # the job's tick always closes the session
        pass


def _null_session_factory() -> _NullSession:
    return _NullSession()


def _wire(loop: SnapshotRefreshJob) -> tuple[JobRegistry, RegisteredScheduler]:
    registry = JobRegistry()
    scheduler = RegisteredScheduler(
        user_id="ariel", settings=_DummySettings(), registry=registry
    )
    registry.bind_scheduler(scheduler)
    scheduler.register_loop(loop)
    registry.register(job=loop, metadata=snapshot_refresh_metadata())
    return registry, scheduler


# ---------------------------------------------------------------------------
# Registration + metadata
# ---------------------------------------------------------------------------


def test_app_registers_snapshot_refresh_enabled() -> None:
    """create_app must register the job enabled=True (Ariel 2026-07-08 go).

    Source-level tripwire: spinning the full app + scheduler per test is
    the known-slow path, and the registration literal is the contract.
    """
    from argosy.api import main as main_mod

    src = inspect.getsource(main_mod)
    m = re.search(r"SnapshotRefreshJob\(enabled=(True|False)", src)
    assert m is not None, "snapshot_refresh registration missing from main.py"
    assert m.group(1) == "True"


def test_metadata_schedule_reads_correctly() -> None:
    meta = snapshot_refresh_metadata()
    assert meta.name == "snapshot_refresh"
    assert meta.schedule_cron == "0 8 * * *"
    assert "manual-only" not in meta.schedule_human
    assert "08:00" in meta.schedule_human


def test_job_defaults_to_jerusalem_cron() -> None:
    job = SnapshotRefreshJob(enabled=True, user_id="ariel")
    assert job.enabled is True
    assert job.schedule.cron == "0 8 * * *"
    assert job.schedule.timezone == "Asia/Jerusalem"


# ---------------------------------------------------------------------------
# job_runs wiring (scheduler seam)
# ---------------------------------------------------------------------------


class _FakeResult:
    """Duck-typed RefreshResult: only .summary() is read by the job."""

    def __init__(self, summary: dict) -> None:
        self._summary = summary

    def summary(self) -> dict:
        return self._summary


@pytest.mark.asyncio
async def test_tick_failure_lands_job_runs_status_error(engine: None) -> None:
    def _boom(session, *, user_id):
        raise RuntimeError("quote backend down")

    loop = SnapshotRefreshJob(
        enabled=True,
        user_id="ariel",
        session_factory=_null_session_factory,
        refresh_fn=_boom,
    )
    registry, _ = _wire(loop)

    with pytest.raises(RuntimeError, match="quote backend down"):
        await registry.fire_now("snapshot_refresh", triggered_by="user:test")

    async with db_mod.get_session() as s:
        row = (
            await s.execute(
                select(JobRun).where(JobRun.job_name == "snapshot_refresh")
            )
        ).scalar_one()
        assert row.status == "error"
        assert "quote backend down" in (row.error_message or "")


@pytest.mark.asyncio
async def test_tick_success_enumerates_misses_in_output_summary(
    engine: None,
) -> None:
    summary = {
        "old_snapshot_date": "2026-07-07",
        "old_total_usd_k": 3979.0,
        "new_total_usd_k": 4015.99,
        "repriced": 42,
        "carried": 7,
        "warnings": ["reprice_miss:SPCX", "fx_miss:usd_eur"],
        "fx_usd_nis": 3.04,
        "fx_usd_eur": 0.87,
    }
    loop = SnapshotRefreshJob(
        enabled=True,
        user_id="ariel",
        session_factory=_null_session_factory,
        refresh_fn=lambda session, *, user_id: _FakeResult(summary),
    )
    registry, _ = _wire(loop)

    run_id = await registry.fire_now("snapshot_refresh", triggered_by="user:test")

    assert loop.last_output_summary == summary
    async with db_mod.get_session() as s:
        row = (
            await s.execute(select(JobRun).where(JobRun.id == run_id))
        ).scalar_one()
        assert row.status == "ok"
        stored = json.loads(row.output_summary)
        assert stored["repriced"] == 42
        # miss symbols reviewable from /admin jobs
        assert "reprice_miss:SPCX" in stored["warnings"]
        assert "fx_miss:usd_eur" in stored["warnings"]


# ---------------------------------------------------------------------------
# Regression: refresh must run OFF the event loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_tick_runs_refresh_off_the_event_loop() -> None:
    """`asyncio.run()` inside the default quote fn must work — i.e. the
    worker thread must have NO running loop (live incident 2026-07-07:
    an on-loop call made all 43 priceable positions a miss)."""
    seen: dict = {}

    def _probe(session, *, user_id):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return _FakeResult({"repriced": 0, "carried": 0, "warnings": []})

    loop = SnapshotRefreshJob(
        enabled=True, user_id="ariel",
        session_factory=_null_session_factory, refresh_fn=_probe,
    )
    await loop.tick()
    assert seen["on_loop"] is False


@pytest.mark.asyncio
async def test_critique_reconcile_default_refresher_runs_off_loop(
    monkeypatch,
) -> None:
    """Same regression for the reconcile loop's `refresh_snapshot` closer
    path — the previous ``session.run_sync`` shape ran the sync service ON
    the loop thread and every quote/FX call failed."""
    from argosy.services import critique_reconcile as cr

    seen: dict = {}

    def _probe(session, *, user_id):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        seen["user_id"] = user_id

    class _S:
        def close(self) -> None:
            seen["closed"] = True

    monkeypatch.setattr(
        "argosy.services.snapshot_refresh.refresh_portfolio_snapshot", _probe
    )
    monkeypatch.setattr(
        "argosy.services.jobs.snapshot_refresh_job._build_default_session_factory",
        lambda: (lambda: _S()),
    )

    await cr._default_snapshot_refresher("ariel")
    assert seen == {"on_loop": False, "user_id": "ariel", "closed": True}
