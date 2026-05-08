"""Tests for plan_amendment workers (Wave 4).

Workers are sync (called via asyncio.to_thread). They:
  - Read the existing current plan and pending draft (if any)
  - Run synthesis (Phase 3 only for medium; full 5-phase for large)
  - Persist a role=draft PlanVersion
  - Update the DecisionRun row with finished_at + status
  - Emit plan.amendment.completed via publish_event_threadsafe
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import DecisionRun, PlanVersion, User


@pytest.fixture
def session_with_baseline_and_run(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.add(PlanVersion(
        user_id="ariel", role="baseline", version_label="x", raw_markdown="# Plan",
        distillate_rendered="# Plan distillate",
    ))
    s.add(PlanVersion(
        user_id="ariel", role="current", version_label="prior",
        raw_markdown="",
        horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
        horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x"}',
        horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"no_change","posture":"x"}',
    ))
    run = DecisionRun(
        user_id="ariel", ticker="(plan)", tier="medium",
        decision_kind="plan_amendment_chat", status="running",
    )
    s.add(run)
    s.commit()
    s.refresh(run)
    yield s, run
    s.close()


def test_medium_worker_calls_synthesizer_with_guidance_and_prior(
    session_with_baseline_and_run, monkeypatch,
):
    from argosy.orchestrator.flows.plan_amendment import workers

    sess, run = session_with_baseline_and_run
    captured = {}

    def _fake_run_phase_3(**kw):
        captured.update(kw)
        from argosy.agents.plan_synthesizer_types import (
            HorizonSection, PlanSynthesisOutput, SynthesisInputs,
        )
        long_h = HorizonSection(
            horizon="long", freshness_expected="annual", status="no_change", posture="x"
        )
        med = HorizonSection(
            horizon="medium", freshness_expected="quarterly", status="minor_revision", posture="x",
        )
        short = HorizonSection(
            horizon="short", freshness_expected="monthly", status="no_change", posture="x"
        )
        return PlanSynthesisOutput(
            long=long_h, medium=med, short=short, inputs=SynthesisInputs(),
        )

    monkeypatch.setattr(workers, "_run_phase_3_synthesizer", _fake_run_phase_3)

    workers._medium_worker(
        session=sess, user_id="ariel", decision_run=run,
        guidance="tighten NVDA toward 12%",
    )

    sess.refresh(run)
    assert run.status == "completed"
    assert run.finished_at is not None
    assert "tighten NVDA" in captured["guidance"]
    # Should have prior_current_md populated from the existing role=current row
    assert "no_change" in captured["prior_current_md"] or len(captured["prior_current_md"]) > 0


def test_medium_worker_writes_role_draft(session_with_baseline_and_run, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment import workers

    sess, run = session_with_baseline_and_run

    def _fake_run_phase_3(**kw):
        from argosy.agents.plan_synthesizer_types import (
            HorizonSection, PlanSynthesisOutput, SynthesisInputs,
        )
        return PlanSynthesisOutput(
            long=HorizonSection(horizon="long", freshness_expected="annual", status="no_change", posture="x"),
            medium=HorizonSection(horizon="medium", freshness_expected="quarterly", status="minor_revision", posture="x"),
            short=HorizonSection(horizon="short", freshness_expected="monthly", status="no_change", posture="x"),
            inputs=SynthesisInputs(),
        )

    monkeypatch.setattr(workers, "_run_phase_3_synthesizer", _fake_run_phase_3)

    workers._medium_worker(
        session=sess, user_id="ariel", decision_run=run, guidance="x",
    )

    drafts = sess.query(PlanVersion).filter_by(user_id="ariel", role="draft").all()
    assert len(drafts) == 1
    assert drafts[0].decision_run_id == run.id


def test_medium_worker_emits_completed_event(session_with_baseline_and_run, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment import workers

    sess, run = session_with_baseline_and_run
    events = []

    def _fake_publish(name, payload):
        events.append((name, payload))

    monkeypatch.setattr(workers, "publish_event_threadsafe", _fake_publish)
    monkeypatch.setattr(workers, "_run_phase_3_synthesizer", lambda **kw: _stub_output())

    workers._medium_worker(session=sess, user_id="ariel", decision_run=run, guidance="x")

    names = [e[0] for e in events]
    assert "plan.amendment.completed" in names


def test_medium_worker_bails_when_cancelled(session_with_baseline_and_run, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment import workers

    sess, run = session_with_baseline_and_run
    run.status = "cancelled"
    sess.commit()

    monkeypatch.setattr(workers, "_run_phase_3_synthesizer", lambda **kw: _stub_output())

    workers._medium_worker(session=sess, user_id="ariel", decision_run=run, guidance="x")

    drafts = sess.query(PlanVersion).filter_by(user_id="ariel", role="draft").all()
    assert len(drafts) == 0


def test_large_worker_delegates_to_run_synthesis(session_with_baseline_and_run, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment import workers

    sess, run = session_with_baseline_and_run
    captured = {}

    def _fake_run_synthesis(session, **kw):
        captured.update(kw)
        from argosy.orchestrator.flows.plan_synthesis import SynthesisResult
        return SynthesisResult(decision_run_id=run.id, draft_id=12345)

    monkeypatch.setattr(workers, "run_synthesis", _fake_run_synthesis)

    workers._large_worker(
        session=sess, user_id="ariel", decision_run=run, guidance="re-evaluate everything",
    )

    assert captured["trigger"] == "check_in"
    assert captured["guidance"] == "re-evaluate everything"
    sess.refresh(run)
    assert run.status == "completed"


def _stub_output():
    from argosy.agents.plan_synthesizer_types import (
        HorizonSection, PlanSynthesisOutput, SynthesisInputs,
    )
    return PlanSynthesisOutput(
        long=HorizonSection(horizon="long", freshness_expected="annual", status="no_change", posture="x"),
        medium=HorizonSection(horizon="medium", freshness_expected="quarterly", status="minor_revision", posture="x"),
        short=HorizonSection(horizon="short", freshness_expected="monthly", status="no_change", posture="x"),
        inputs=SynthesisInputs(),
    )
