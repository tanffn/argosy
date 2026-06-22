"""Tests for P3 (plan freshness) + P5 (north-star verify)."""
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.decision_funnel.north_star import assess_alignment
from argosy.services.decision_funnel.plan_freshness import (
    STALE_AGE_DAYS,
    detect_plan_freshness,
)
from argosy.services import funnel_trace as ft
from argosy.state.models import Base, PlanVersion, User

NOW = datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc)
DAY = "2026-06-22"


@pytest.fixture
def session():
    eng = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng, expire_on_commit=False)
    s = SF()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


# ---- P5 north-star ----


def test_north_star_aligned_for_risk_reducing_sell():
    v = assess_alignment(triggers=["thesis_broken"], action="sell")
    assert v.aligned
    assert "risk" in v.justification.lower()


def test_north_star_aligned_for_earnings_event():
    v = assess_alignment(triggers=["earnings_imminent"], action="buy")
    assert v.aligned


def test_north_star_not_aligned_for_audit_only():
    v = assess_alignment(triggers=["audit_sample"], action="sell")
    assert not v.aligned
    assert "audit" in v.justification.lower()


def test_north_star_not_aligned_when_no_triggers():
    v = assess_alignment(triggers=[], action="hold")
    assert not v.aligned


# ---- P3 plan freshness ----


def _add_plan(session, *, accepted_at):
    pv = PlanVersion(user_id="ariel", role="current")
    # accepted_at may not be a constructor arg on all schemas; set if present.
    if hasattr(pv, "accepted_at"):
        pv.accepted_at = accepted_at
    session.add(pv)
    session.commit()
    return pv


def test_plan_freshness_no_plan(session):
    pf = detect_plan_freshness(session, user_id="ariel", now=NOW)
    assert pf.has_plan is False
    assert pf.refresh_recommended is False


def test_plan_freshness_current_no_change(session):
    _add_plan(session, accepted_at=NOW - timedelta(days=3))
    pf = detect_plan_freshness(session, user_id="ariel", now=NOW)
    assert pf.has_plan is True
    assert pf.stale is False
    assert pf.refresh_recommended is False
    assert not pf.material_changes


def test_plan_freshness_stale_age(session):
    _add_plan(session, accepted_at=NOW - timedelta(days=STALE_AGE_DAYS + 5))
    pf = detect_plan_freshness(session, user_id="ariel", now=NOW)
    assert pf.stale is True
    assert pf.refresh_recommended is True


def test_plan_freshness_detects_material_change(session):
    _add_plan(session, accepted_at=NOW - timedelta(days=3))
    # A funnel run AFTER the plan with a thesis-break route is a material change.
    run = ft.open_run(
        session, user_id="ariel", day=DAY, shadow=True, policy_version="p",
        ips_version="i", plan_version_id=1, started_at=NOW,
    )
    ft.record_stage_row(
        session, run_id=run.id, stage="stage1", subject="NVDA",
        subject_type="holding", decision="routed",
        reason="hard trigger(s): thesis_broken", signal_or_rule="thesis_broken",
    )
    ft.record_stage_row(
        session, run_id=run.id, stage="stage1", subject="CSPX",
        subject_type="holding", decision="dropped", reason="no material signal",
        signal_or_rule="no_match",
    )
    pf = detect_plan_freshness(session, user_id="ariel", now=NOW)
    assert pf.refresh_recommended is True
    subjects = {m.subject for m in pf.material_changes}
    assert "NVDA" in subjects
    assert "CSPX" not in subjects  # a drop is not a material change
