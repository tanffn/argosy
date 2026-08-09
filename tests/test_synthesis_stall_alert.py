"""Synthesis stall alert — simulated quiet in-flight run → flag + inbox."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.services.synthesis_stall_alert import (
    STALL_KIND,
    write_stall_alerts,
)
from argosy.state.models import ActionProposal, DecisionRun, MonitorFlag, User

NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


def test_stall_writes_flag_and_inbox(session):
    run = DecisionRun(
        user_id="ariel",
        ticker="PLAN",
        decision_kind="plan_revision",
        started_at=NOW - timedelta(hours=2),
        status="running",
    )
    session.add(run)
    session.commit()

    ids = write_stall_alerts(
        session, user_id="ariel", now=NOW, alert_minutes=20,
    )
    session.commit()
    assert ids == [run.id]

    flag = session.query(MonitorFlag).filter_by(
        user_id="ariel", kind=STALL_KIND, status="active",
    ).one()
    assert "no heartbeat" in json_payload_message(flag.payload)
    prop = session.query(ActionProposal).filter_by(
        user_id="ariel", status="open",
    ).one()
    assert "stall" in prop.summary.lower()


def test_fresh_run_not_alerted(session):
    run = DecisionRun(
        user_id="ariel",
        ticker="PLAN",
        decision_kind="plan_revision",
        started_at=NOW - timedelta(minutes=5),
        status="running",
    )
    session.add(run)
    session.commit()
    ids = write_stall_alerts(
        session, user_id="ariel", now=NOW, alert_minutes=20,
    )
    assert ids == []


def json_payload_message(payload: str) -> str:
    import json

    return json.loads(payload).get("message", "")


# ---------------------------------------------------------------------------
# Reliability: the detector must ALERT, never THROW (the run-283 bug).
# ---------------------------------------------------------------------------


def test_write_stall_alerts_survives_persist_failure(session, monkeypatch):
    """A blocked monitor-flag/inbox write must NOT drop the alert.

    The durable alert is the log line, emitted before persistence. If the
    richer persist path raises (e.g. ``database is locked`` under fleet
    contention), ``write_stall_alerts`` still returns the detected run —
    log-first, best-effort persist — and never propagates the exception.
    """
    from sqlalchemy.exc import OperationalError

    import argosy.services.synthesis_stall_alert as mod

    run = DecisionRun(
        user_id="ariel",
        ticker="PLAN",
        decision_kind="plan_revision",
        started_at=NOW - timedelta(hours=2),
        status="running",
    )
    session.add(run)
    session.commit()

    def _boom(*_a, **_k):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(mod, "_persist_stall_alert", _boom)

    # Must NOT raise; must still surface the detected stall.
    ids = write_stall_alerts(session, user_id="ariel", now=NOW, alert_minutes=20)
    assert ids == [run.id]


@pytest.mark.asyncio
async def test_stall_alert_surfaces_when_open_job_run_fails(
    engine, alembic_engine_at_head
):
    """End-to-end: even when the JobRegistry audit-row open throws
    (``jobs.open_job_run_failed`` — the run-283 failure mode under DB
    contention), the scheduled fire must NOT raise and the stall alert
    must still surface (monitor flag + inbox row).
    """
    from argosy.orchestrator.loops.synthesis_stall_alert import (
        SynthesisStallAlertLoop,
        synthesis_stall_alert_metadata,
    )
    from argosy.services.jobs import JobRegistry, RegisteredScheduler

    sync_factory = sessionmaker(
        bind=alembic_engine_at_head, expire_on_commit=False
    )
    seed = sync_factory()
    seed.add(User(id="ariel", plan="free"))
    seed.add(
        DecisionRun(
            user_id="ariel",
            ticker="PLAN",
            decision_kind="plan_revision",
            started_at=datetime.now(timezone.utc) - timedelta(hours=8),
            status="running",
        )
    )
    seed.commit()
    seed.close()

    loop = SynthesisStallAlertLoop(
        enabled=True,
        user_id="ariel",
        session_factory=sync_factory,
        alert_minutes=20,
    )

    class _Settings:  # minimal stand-in; scheduler only reads cadences lazily
        pass

    registry = JobRegistry()
    scheduler = RegisteredScheduler(
        user_id="ariel", settings=_Settings(), registry=registry
    )
    registry.bind_scheduler(scheduler)
    scheduler.register_loop(loop)
    registry.register(job=loop, metadata=synthesis_stall_alert_metadata())

    # Simulate the audit-row open failing under DB contention.
    async def _boom(**_kw):
        raise RuntimeError("database is locked")

    registry._open_job_run = _boom  # type: ignore[assignment]

    # Scheduled entry (force=False). Pre-fix this SILENTLY SKIPPED the tick,
    # so no alert surfaced. It must not raise now — and the alert must land.
    await scheduler._fire_once(loop)

    check = sync_factory()
    flags = (
        check.query(MonitorFlag)
        .filter_by(user_id="ariel", kind=STALL_KIND, status="active")
        .all()
    )
    props = (
        check.query(ActionProposal).filter_by(user_id="ariel", status="open").all()
    )
    check.close()
    assert len(flags) == 1, "stall alert flag must surface despite audit-open failure"
    assert len(props) == 1, "stall alert inbox row must surface despite audit-open failure"


def test_create_sync_engine_sets_busy_timeout(alembic_engine_at_head):
    """Bug 2 root cause: sync engines were built without ``busy_timeout``,
    so kv_cache / stall-alert writes raised ``database is locked`` INSTANTLY
    under contention. ``create_sync_engine`` must apply the 60s busy_timeout
    (matching the async engine) so writers WAIT instead of failing fast.
    """
    from argosy.state.db import SQLITE_BUSY_TIMEOUT_MS, create_sync_engine

    eng = create_sync_engine()
    try:
        with eng.connect() as conn:
            busy = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
            journal = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    finally:
        eng.dispose()
    assert busy == SQLITE_BUSY_TIMEOUT_MS
    assert str(journal).lower() == "wal"
