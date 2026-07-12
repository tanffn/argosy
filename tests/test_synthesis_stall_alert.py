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
