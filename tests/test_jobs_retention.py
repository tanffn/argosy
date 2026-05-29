"""Sprint A commit #9 — tests for :class:`JobRunsRetentionLoop`.

Spec: ``docs/superpowers/specs/2026-05-29-jobs-registry-design.md`` §2.1
+ §1.7 (the orphan-row reap).

Test surface:

* ``status='ok' AND finished_at < now-30d`` is deleted
* ``status='ok' AND finished_at < now-29d`` is NOT deleted (under window)
* ``status='error'`` is NEVER deleted (operator postmortem trail)
* ``status='skipped'`` / ``status='cancelled'`` are NOT deleted by the
  ok-row pass (only ``status='ok'`` is targeted)
* ``status='running' AND started_at < now-24h`` flipped to ``cancelled``
  with the documented ``error_message`` + ``finished_at`` stamped
* ``status='running' AND started_at < now-23h`` NOT reaped (under window)
* ``output_summary`` carries ``deleted_ok_count`` + ``reaped_running_count``
* Idempotent: a second tick in the same wall-clock day is a no-op
* Configurable: passing ``retention_days_ok=7`` / ``stale_running_hours=1``
  shrinks the windows

Test command::

    D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest \\
        -m "not llm_eval" tests/test_jobs_retention.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from argosy.orchestrator.loops.job_runs_retention import (
    JobRunsRetentionLoop,
    job_runs_retention_metadata,
)
from argosy.state import db as db_mod
from argosy.state.models import JobRun


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FROZEN_NOW = datetime(2026, 5, 29, 3, 30, 0, tzinfo=timezone.utc)


def _make_loop(
    *,
    retention_days_ok: int = 30,
    stale_running_hours: int = 24,
    frozen_now: datetime = _FROZEN_NOW,
) -> JobRunsRetentionLoop:
    return JobRunsRetentionLoop(
        retention_days_ok=retention_days_ok,
        stale_running_hours=stale_running_hours,
        now_fn=lambda: frozen_now,
    )


async def _insert_row(
    *,
    job_name: str,
    status: str,
    idempotency_key: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    error_message: str | None = None,
) -> int:
    """Insert a ``job_runs`` row directly (bypasses the registry — this
    is a test-only seed)."""
    async with db_mod.get_session() as session:
        row = JobRun(
            job_name=job_name,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            skip_reason=None,
            error_message=error_message,
            manual_trigger=0,
            triggered_by="seed",
            output_summary=None,
            duration_ms=None,
            idempotency_key=idempotency_key,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _row_count(*, job_name: str | None = None) -> int:
    async with db_mod.get_session() as session:
        stmt = select(JobRun)
        if job_name is not None:
            stmt = stmt.where(JobRun.job_name == job_name)
        rows = (await session.execute(stmt)).scalars().all()
        return len(rows)


async def _get_status(row_id: int) -> str | None:
    async with db_mod.get_session() as session:
        return (
            await session.execute(
                select(JobRun.status).where(JobRun.id == row_id)
            )
        ).scalar_one_or_none()


async def _get_row(row_id: int) -> JobRun | None:
    async with db_mod.get_session() as session:
        return (
            await session.execute(
                select(JobRun).where(JobRun.id == row_id)
            )
        ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Pass 1 — delete old ok rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_deletes_ok_older_than_30d(engine: None) -> None:
    """An ``ok`` row finished 31d ago is pruned."""
    loop = _make_loop()
    # 31d ago → outside the 30d window.
    old_finished = _FROZEN_NOW - timedelta(days=31)
    row_id = await _insert_row(
        job_name="x",
        status="ok",
        idempotency_key="ok-31d",
        started_at=old_finished - timedelta(seconds=1),
        finished_at=old_finished,
    )

    summary = await loop.tick()

    assert summary["deleted_ok_count"] == 1
    assert summary["reaped_running_count"] == 0
    assert await _get_row(row_id) is None


@pytest.mark.asyncio
async def test_retention_keeps_ok_within_30d(engine: None) -> None:
    """An ``ok`` row finished 29d ago is kept."""
    loop = _make_loop()
    recent_finished = _FROZEN_NOW - timedelta(days=29)
    row_id = await _insert_row(
        job_name="x",
        status="ok",
        idempotency_key="ok-29d",
        started_at=recent_finished - timedelta(seconds=1),
        finished_at=recent_finished,
    )

    summary = await loop.tick()

    assert summary["deleted_ok_count"] == 0
    assert await _get_status(row_id) == "ok"


@pytest.mark.asyncio
async def test_retention_never_deletes_error_rows(engine: None) -> None:
    """``status='error'`` is kept FOREVER. Even a 365-day-old error row
    survives the prune so the operator can postmortem from the audit log.
    """
    loop = _make_loop()
    ancient = _FROZEN_NOW - timedelta(days=365)
    err_id = await _insert_row(
        job_name="x",
        status="error",
        idempotency_key="err-365d",
        started_at=ancient,
        finished_at=ancient + timedelta(seconds=1),
        error_message="boom",
    )

    summary = await loop.tick()

    assert summary["deleted_ok_count"] == 0
    # Error row survives the 30d delete pass.
    assert await _get_status(err_id) == "error"


@pytest.mark.asyncio
async def test_retention_does_not_delete_skipped_or_cancelled(
    engine: None,
) -> None:
    """Only ``status='ok'`` is targeted by Pass 1. ``skipped`` /
    ``cancelled`` rows older than 30d are NOT touched (cheap to retain;
    useful for debugging "why didn't this fire?").
    """
    loop = _make_loop()
    ancient = _FROZEN_NOW - timedelta(days=100)
    skipped_id = await _insert_row(
        job_name="x",
        status="skipped",
        idempotency_key="skip-100d",
        started_at=ancient,
        finished_at=ancient + timedelta(seconds=1),
    )
    cancelled_id = await _insert_row(
        job_name="x",
        status="cancelled",
        idempotency_key="cxl-100d",
        started_at=ancient,
        finished_at=ancient + timedelta(seconds=1),
    )

    summary = await loop.tick()

    assert summary["deleted_ok_count"] == 0
    assert await _get_status(skipped_id) == "skipped"
    assert await _get_status(cancelled_id) == "cancelled"


# ---------------------------------------------------------------------------
# Pass 2 — reap stale running rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_reaps_running_older_than_24h(engine: None) -> None:
    """``status='running' AND started_at < now-24h`` is flipped to
    ``cancelled`` with the documented ``error_message`` + ``finished_at``
    stamp.
    """
    loop = _make_loop()
    stale_started = _FROZEN_NOW - timedelta(hours=25)
    row_id = await _insert_row(
        job_name="x",
        status="running",
        idempotency_key="running-25h",
        started_at=stale_started,
        finished_at=None,
    )

    summary = await loop.tick()

    assert summary["reaped_running_count"] == 1
    assert summary["deleted_ok_count"] == 0

    row = await _get_row(row_id)
    assert row is not None
    assert row.status == "cancelled"
    assert row.error_message == "reaped: stale running row"
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_retention_does_not_reap_recent_running(engine: None) -> None:
    """A row that started 23h ago is BELOW the 24h reap threshold."""
    loop = _make_loop()
    recent_started = _FROZEN_NOW - timedelta(hours=23)
    row_id = await _insert_row(
        job_name="x",
        status="running",
        idempotency_key="running-23h",
        started_at=recent_started,
        finished_at=None,
    )

    summary = await loop.tick()

    assert summary["reaped_running_count"] == 0
    row = await _get_row(row_id)
    assert row is not None
    assert row.status == "running"


# ---------------------------------------------------------------------------
# Output summary + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_summary_carries_counts(engine: None) -> None:
    """``output_summary`` carries both counts + window settings."""
    loop = _make_loop(retention_days_ok=30, stale_running_hours=24)

    # Seed: 2 deletable ok rows + 1 reapable running row.
    await _insert_row(
        job_name="x",
        status="ok",
        idempotency_key="ok-old-1",
        started_at=_FROZEN_NOW - timedelta(days=40),
        finished_at=_FROZEN_NOW - timedelta(days=40),
    )
    await _insert_row(
        job_name="x",
        status="ok",
        idempotency_key="ok-old-2",
        started_at=_FROZEN_NOW - timedelta(days=35),
        finished_at=_FROZEN_NOW - timedelta(days=35),
    )
    await _insert_row(
        job_name="x",
        status="running",
        idempotency_key="run-old-1",
        started_at=_FROZEN_NOW - timedelta(hours=48),
        finished_at=None,
    )

    summary = await loop.tick()
    assert summary["deleted_ok_count"] == 2
    assert summary["reaped_running_count"] == 1
    assert summary["windows"]["retention_days_ok"] == 30
    assert summary["windows"]["stale_running_hours"] == 24
    # Side-channel mirrors the return value (RegisteredScheduler reads
    # the attribute as a fallback on the exception path).
    assert loop.last_output_summary == summary


@pytest.mark.asyncio
async def test_retention_is_idempotent(engine: None) -> None:
    """Running retention twice on the same wall clock finds no work
    the second time — no rows raised, no errors, both counts zero.
    """
    loop = _make_loop()
    await _insert_row(
        job_name="x",
        status="ok",
        idempotency_key="ok-old-idem",
        started_at=_FROZEN_NOW - timedelta(days=45),
        finished_at=_FROZEN_NOW - timedelta(days=45),
    )
    await _insert_row(
        job_name="x",
        status="running",
        idempotency_key="run-old-idem",
        started_at=_FROZEN_NOW - timedelta(hours=48),
        finished_at=None,
    )

    first = await loop.tick()
    assert first["deleted_ok_count"] == 1
    assert first["reaped_running_count"] == 1

    second = await loop.tick()
    # Pass 1 finds no deletable rows (the ok row is gone).
    # Pass 2 finds no reapable rows (the running row is now 'cancelled').
    assert second["deleted_ok_count"] == 0
    assert second["reaped_running_count"] == 0


# ---------------------------------------------------------------------------
# Configurability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_window_is_configurable(engine: None) -> None:
    """Passing ``retention_days_ok=7`` + ``stale_running_hours=1``
    deletes a 10d-old ok row that the default 30d window would keep,
    and reaps a 2h-old running row that the default 24h window would
    keep — verifying the windows are actually plumbed into the SQL.
    """
    loop = _make_loop(retention_days_ok=7, stale_running_hours=1)

    ok_id = await _insert_row(
        job_name="x",
        status="ok",
        idempotency_key="ok-10d",
        started_at=_FROZEN_NOW - timedelta(days=10),
        finished_at=_FROZEN_NOW - timedelta(days=10),
    )
    running_id = await _insert_row(
        job_name="x",
        status="running",
        idempotency_key="run-2h",
        started_at=_FROZEN_NOW - timedelta(hours=2),
        finished_at=None,
    )

    summary = await loop.tick()
    assert summary["deleted_ok_count"] == 1
    assert summary["reaped_running_count"] == 1
    assert await _get_row(ok_id) is None
    row = await _get_row(running_id)
    assert row is not None
    assert row.status == "cancelled"


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_metadata_shape() -> None:
    """The metadata for the retention loop matches §6's mapping (kind
    ``maintenance``, scheduled-not-long-running)."""
    md = job_runs_retention_metadata()
    assert md.name == "job_runs_retention"
    assert md.schedule_cron == "30 3 * * *"
    assert md.source_kind == "maintenance"
    assert md.long_running is False


def test_constructor_rejects_negative_windows() -> None:
    with pytest.raises(ValueError, match="retention_days_ok"):
        JobRunsRetentionLoop(retention_days_ok=-1)
    with pytest.raises(ValueError, match="stale_running_hours"):
        JobRunsRetentionLoop(stale_running_hours=-1)


# ---------------------------------------------------------------------------
# Settings plumbing
# ---------------------------------------------------------------------------


def test_default_yaml_documents_retention_blocks() -> None:
    """Codex IMPORTANT #1 — the auto-generated default YAML scaffold
    documents both retention blocks so a user with a fresh config can
    discover the knobs without reading code.
    """
    from argosy.agent_settings import _DEFAULT_YAML

    assert "job_runs_retention:" in _DEFAULT_YAML
    # Both the cadence entry AND the top-level tuning block must be
    # present (the substring appears at least twice in the scaffold).
    assert _DEFAULT_YAML.count("job_runs_retention") >= 2
    assert "retention_days_ok" in _DEFAULT_YAML
    assert "stale_running_hours" in _DEFAULT_YAML


# ---------------------------------------------------------------------------
# Codex BLOCKER #1 — LongRunningJob rows EXCLUDED from the reap pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_excludes_long_running_rows_from_reap(
    engine: None,
) -> None:
    """A healthy LongRunningJob row (e.g. Discord listener) whose
    ``started_at`` is >24h ago must NOT be reaped — the connection is
    legitimately long-lived. The exclusion is keyed by the
    ``long_running_names_fn`` callable supplied at construction.
    """
    loop = JobRunsRetentionLoop(
        retention_days_ok=30,
        stale_running_hours=24,
        now_fn=lambda: _FROZEN_NOW,
        long_running_names_fn=lambda: ["discord_listener"],
    )

    # Seed: one long-running supervisor row (48h old) + one CadenceLoop
    # orphan row (also 48h old). Only the orphan should be reaped.
    lr_id = await _insert_row(
        job_name="discord_listener",
        status="running",
        idempotency_key="lr-running-48h",
        started_at=_FROZEN_NOW - timedelta(hours=48),
        finished_at=None,
    )
    orphan_id = await _insert_row(
        job_name="some_cadence_loop",
        status="running",
        idempotency_key="cad-running-48h",
        started_at=_FROZEN_NOW - timedelta(hours=48),
        finished_at=None,
    )

    summary = await loop.tick()

    # Exactly ONE reap — the orphan CadenceLoop row.
    assert summary["reaped_running_count"] == 1
    assert summary["excluded_long_running"] == ["discord_listener"]

    # LongRunningJob row survives in 'running' state.
    lr_row = await _get_row(lr_id)
    assert lr_row is not None
    assert lr_row.status == "running"
    # CadenceLoop orphan row was reaped.
    orphan_row = await _get_row(orphan_id)
    assert orphan_row is not None
    assert orphan_row.status == "cancelled"
    assert orphan_row.error_message == "reaped: stale running row"


@pytest.mark.asyncio
async def test_retention_default_no_long_running_exclusion(
    engine: None,
) -> None:
    """When no ``long_running_names_fn`` is supplied, the exclusion set
    is empty — preserves the legacy behavior + the existing tests'
    assumptions.
    """
    loop = _make_loop()  # No long_running_names_fn.
    row_id = await _insert_row(
        job_name="discord_listener",
        status="running",
        idempotency_key="lr-default",
        started_at=_FROZEN_NOW - timedelta(hours=48),
        finished_at=None,
    )
    summary = await loop.tick()
    assert summary["reaped_running_count"] == 1
    assert summary["excluded_long_running"] == []
    row = await _get_row(row_id)
    assert row is not None
    assert row.status == "cancelled"


# ---------------------------------------------------------------------------
# Codex IMPORTANT #2 — partial-failure atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_rolls_back_on_update_failure(
    engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Pass 2 (UPDATE) fails, Pass 1 (DELETE) must NOT have persisted
    — both writes happen inside the same async session before commit,
    so an exception aborts the transaction.
    """
    loop = _make_loop()

    # Seed: one deletable ok row + one reapable running row.
    ok_id = await _insert_row(
        job_name="x",
        status="ok",
        idempotency_key="ok-atomic-1",
        started_at=_FROZEN_NOW - timedelta(days=40),
        finished_at=_FROZEN_NOW - timedelta(days=40),
    )
    running_id = await _insert_row(
        job_name="x",
        status="running",
        idempotency_key="run-atomic-1",
        started_at=_FROZEN_NOW - timedelta(hours=48),
        finished_at=None,
    )

    # Patch session.execute on the SECOND call inside tick() (Pass 2)
    # to raise. The first call (Pass 1 DELETE) runs normally; the raise
    # propagates so the surrounding ``async with db_mod.get_session``
    # block rolls back without committing.
    from sqlalchemy.ext.asyncio import AsyncSession

    real_execute = AsyncSession.execute
    call_count = {"n": 0}

    async def _flaky_execute(self, statement, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated UPDATE failure")
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", _flaky_execute)

    with pytest.raises(RuntimeError, match="simulated UPDATE failure"):
        await loop.tick()

    # BOTH writes must have rolled back:
    #   * the ok row is STILL present (DELETE was rolled back)
    #   * the running row is STILL in 'running' (UPDATE never persisted)
    ok_row = await _get_row(ok_id)
    assert ok_row is not None, (
        "DELETE was NOT rolled back when UPDATE failed — "
        "single-transaction guarantee violated"
    )
    assert ok_row.status == "ok"

    running_row = await _get_row(running_id)
    assert running_row is not None
    assert running_row.status == "running"


# ---------------------------------------------------------------------------
# Settings plumbing (continued)
# ---------------------------------------------------------------------------


def test_agent_settings_has_retention_config() -> None:
    """``agent_settings.AgentSettings`` exposes the retention windows
    so the startup hook can pass them into the loop constructor.
    """
    from argosy.agent_settings import AgentSettings

    s = AgentSettings()
    # Cadence block has the schedule.
    assert s.cadences.job_runs_retention.cron == "30 3 * * *"
    assert s.cadences.job_runs_retention.timezone == "Asia/Jerusalem"
    assert s.cadences.job_runs_retention.enabled is True
    # Top-level block has the window tuning.
    assert s.job_runs_retention.retention_days_ok == 30
    assert s.job_runs_retention.stale_running_hours == 24
