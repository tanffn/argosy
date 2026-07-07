"""Tests for the stale plan-synthesis liveness reaper.

Covers the service (``argosy/services/synthesis_liveness.py``) plus the
``GET /api/plan/in-flight-synthesis`` wiring: a zombie 'running'
plan_revision run must never be reported as in flight — the endpoint
call itself reaps it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argosy.services.synthesis_liveness import (
    DEFAULT_STALE_MINUTES,
    reap_stale_synthesis_runs,
)
from argosy.state.models import DecisionPhase, DecisionRun, JobRun, User

NOW = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)


def _seed_user(SF, user_id: str = "ariel") -> None:
    with SF() as s:
        if s.get(User, user_id) is None:
            s.add(User(id=user_id))
            s.commit()


def _seed_run(
    SF,
    *,
    user_id: str = "ariel",
    started_at: datetime,
    status: str = "running",
    decision_kind: str = "plan_revision",
) -> int:
    with SF() as s:
        run = DecisionRun(
            user_id=user_id,
            ticker="PLAN",
            decision_kind=decision_kind,
            started_at=started_at,
            status=status,
        )
        s.add(run)
        s.commit()
        return run.id


def _seed_phase(SF, *, run_id: int, seq: int, started_at: datetime,
                finished_at: datetime | None) -> None:
    with SF() as s:
        s.add(
            DecisionPhase(
                decision_run_id=run_id,
                user_id="ariel",
                seq=seq,
                kind=f"synthesis.phase_{seq}",
                started_at=started_at,
                finished_at=finished_at,
                participants_json="[]",
            )
        )
        s.commit()


def _seed_job_run(SF, *, job_name: str, started_at: datetime,
                  status: str = "running") -> int:
    with SF() as s:
        jr = JobRun(
            job_name=job_name,
            started_at=started_at,
            status=status,
            idempotency_key=f"test|{job_name}|{started_at.isoformat()}",
        )
        s.add(jr)
        s.commit()
        return jr.id


class TestReapService:
    def test_stale_run_without_phases_is_reaped(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        run_id = _seed_run(SF, started_at=NOW - timedelta(hours=4))

        with SF() as s:
            reaped = reap_stale_synthesis_runs(s, user_id="ariel", now=NOW)
        assert reaped == [run_id]

        with SF() as s:
            run = s.get(DecisionRun, run_id)
            assert run.status == "failed"
            assert run.finished_at is not None
            assert "reaped" in (run.notes_json or "")

    def test_run_with_recent_phase_activity_survives(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        # Run started hours ago but a phase finished 5 minutes ago.
        run_id = _seed_run(SF, started_at=NOW - timedelta(hours=4))
        _seed_phase(
            SF, run_id=run_id, seq=1,
            started_at=NOW - timedelta(hours=4),
            finished_at=NOW - timedelta(minutes=5),
        )

        with SF() as s:
            reaped = reap_stale_synthesis_runs(s, user_id="ariel", now=NOW)
        assert reaped == []
        with SF() as s:
            assert s.get(DecisionRun, run_id).status == "running"

    def test_run_with_only_stale_phases_is_reaped(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        run_id = _seed_run(SF, started_at=NOW - timedelta(hours=4))
        _seed_phase(
            SF, run_id=run_id, seq=1,
            started_at=NOW - timedelta(hours=4),
            finished_at=NOW - timedelta(hours=3, minutes=40),
        )
        with SF() as s:
            reaped = reap_stale_synthesis_runs(s, user_id="ariel", now=NOW)
        assert reaped == [run_id]

    def test_fresh_run_survives(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        run_id = _seed_run(
            SF, started_at=NOW - timedelta(minutes=DEFAULT_STALE_MINUTES - 5)
        )
        with SF() as s:
            reaped = reap_stale_synthesis_runs(s, user_id="ariel", now=NOW)
        assert reaped == []
        with SF() as s:
            assert s.get(DecisionRun, run_id).status == "running"

    def test_non_plan_revision_runs_untouched(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        run_id = _seed_run(
            SF,
            started_at=NOW - timedelta(hours=8),
            decision_kind="trade_proposal",
        )
        with SF() as s:
            reaped = reap_stale_synthesis_runs(s, user_id="ariel", now=NOW)
        assert reaped == []
        with SF() as s:
            assert s.get(DecisionRun, run_id).status == "running"

    def test_wrapper_job_run_is_closed_with_the_run(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        run_started = NOW - timedelta(hours=4)
        run_id = _seed_run(SF, started_at=run_started)
        # Wrapper opened sub-second before the decision run (real shape).
        wrapper_id = _seed_job_run(
            SF, job_name="monthly_cycle",
            started_at=run_started - timedelta(seconds=1),
        )
        # Unrelated running job — different name — must survive.
        other_id = _seed_job_run(
            SF, job_name="discord_listener",
            started_at=run_started,
        )
        # Same wrapper name but far away in time — must survive.
        far_id = _seed_job_run(
            SF, job_name="monthly_cycle",
            started_at=run_started - timedelta(hours=2),
        )

        with SF() as s:
            reaped = reap_stale_synthesis_runs(s, user_id="ariel", now=NOW)
        assert reaped == [run_id]

        with SF() as s:
            wrapper = s.get(JobRun, wrapper_id)
            assert wrapper.status == "cancelled"
            assert f"run {run_id}" in (wrapper.error_message or "")
            assert wrapper.finished_at is not None
            assert s.get(JobRun, other_id).status == "running"
            assert s.get(JobRun, far_id).status == "running"

    def test_idempotent(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        _seed_run(SF, started_at=NOW - timedelta(hours=4))
        with SF() as s:
            first = reap_stale_synthesis_runs(s, user_id="ariel", now=NOW)
        with SF() as s:
            second = reap_stale_synthesis_runs(s, user_id="ariel", now=NOW)
        assert len(first) == 1
        assert second == []


class TestInFlightEndpointReaps:
    def test_zombie_run_reports_null_and_is_reaped(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        # Started long ago, no phases — the run 134/135 shape.
        run_id = _seed_run(
            SF,
            started_at=datetime.now(timezone.utc) - timedelta(hours=6),
        )

        r = client_with_db.get("/api/plan/in-flight-synthesis?user_id=ariel")
        assert r.status_code == 200
        assert r.json()["in_flight_synthesis"] is None

        with SF() as s:
            assert s.get(DecisionRun, run_id).status == "failed"

    def test_live_run_still_reported_in_flight(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        run_id = _seed_run(SF, started_at=datetime.now(timezone.utc))

        r = client_with_db.get("/api/plan/in-flight-synthesis?user_id=ariel")
        assert r.status_code == 200
        body = r.json()["in_flight_synthesis"]
        assert body is not None
        assert body["decision_run_id"] == run_id
        assert body["status"] == "running"
