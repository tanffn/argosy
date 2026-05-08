"""Tests for plan_amendment workers (Wave 4).

Workers are sync (called via asyncio.to_thread). They:
  - Read the existing current plan and pending draft (if any)
  - Run synthesis (Phase 3 only for medium; full 5-phase for large)
  - Persist a role=draft PlanVersion
  - Update the DecisionRun row with finished_at + status
  - Emit plan.amendment.completed via publish_event_threadsafe
"""

from __future__ import annotations

import json

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
    # I1: the worker MUST pass its own DecisionRun id through so the
    # synthesis reuses the row instead of opening a parallel one.
    assert captured["existing_decision_run_id"] == run.id
    sess.refresh(run)
    assert run.status == "completed"


def test_large_worker_lineage_attaches_real_draft(session_with_baseline_and_run, monkeypatch):
    """M6 / I1: when run_synthesis returns a SynthesisResult pointing at
    a real PlanVersion in the same session, the worker should leave the
    DecisionRun in `completed` and the row should retain its
    plan_amendment_chat kind (not get rewritten by the synthesis path).
    """
    from argosy.orchestrator.flows.plan_amendment import workers

    sess, run = session_with_baseline_and_run

    # Pretend the synthesis flow created a real draft attached to the
    # worker's DecisionRun (the I1 refactor makes this how things really
    # work — synthesis writes against existing_decision_run_id).
    real_draft = PlanVersion(
        user_id="ariel", role="draft", version_label="amend-large-test",
        source_path="", raw_markdown="", decision_run_id=run.id,
    )
    sess.add(real_draft)
    sess.commit()
    sess.refresh(real_draft)
    real_draft_id = real_draft.id

    def _fake_run_synthesis(session, **kw):
        from argosy.orchestrator.flows.plan_synthesis import SynthesisResult
        # Synthesis (with existing_decision_run_id) does NOT stamp the
        # row as completed; the worker is expected to do that.
        return SynthesisResult(decision_run_id=run.id, draft_id=real_draft_id)

    monkeypatch.setattr(workers, "run_synthesis", _fake_run_synthesis)

    events = []
    monkeypatch.setattr(
        workers, "publish_event_threadsafe",
        lambda name, payload: events.append((name, payload)),
    )

    workers._large_worker(
        session=sess, user_id="ariel", decision_run=run, guidance="x",
    )

    sess.refresh(run)
    assert run.status == "completed"
    assert run.decision_kind == "plan_amendment_chat"
    sess.refresh(real_draft)
    assert real_draft.decision_run_id == run.id

    # And the completion event names the right draft.
    completed = [e for e in events if e[0] == "plan.amendment.completed"]
    assert completed
    assert completed[0][1]["draft_id"] == real_draft_id


def test_large_worker_cancelled_during_run_does_not_overwrite_status(
    session_with_baseline_and_run, monkeypatch,
):
    """I5: cancellation can land mid-synthesis. Worker must re-check
    status after run_synthesis and bail without stamping completed."""
    from argosy.orchestrator.flows.plan_amendment import workers

    sess, run = session_with_baseline_and_run

    def _fake_run_synthesis(session, **kw):
        # Simulate cancellation arriving during the long synthesis call.
        run_in_session = session.get(DecisionRun, run.id)
        run_in_session.status = "cancelled"
        from datetime import datetime, timezone
        run_in_session.finished_at = datetime.now(timezone.utc)
        session.commit()
        from argosy.orchestrator.flows.plan_synthesis import SynthesisResult
        return SynthesisResult(decision_run_id=run.id, draft_id=999)

    monkeypatch.setattr(workers, "run_synthesis", _fake_run_synthesis)

    events = []
    monkeypatch.setattr(
        workers, "publish_event_threadsafe",
        lambda name, payload: events.append((name, payload)),
    )

    workers._large_worker(
        session=sess, user_id="ariel", decision_run=run, guidance="x",
    )

    sess.refresh(run)
    # Status must remain cancelled; worker must NOT have overwritten it.
    assert run.status == "cancelled"

    names = [e[0] for e in events]
    assert "plan.amendment.cancelled" in names
    assert "plan.amendment.completed" not in names


def test_medium_worker_failure_preserves_original_notes(
    session_with_baseline_and_run, monkeypatch,
):
    """I2: on worker failure, the error must be MERGED into notes_json
    (not overwriting the original message + parsed intent the dispatcher
    wrote for replay)."""
    from argosy.orchestrator.flows.plan_amendment import workers

    sess, run = session_with_baseline_and_run
    # Simulate the dispatcher's notes payload.
    run.notes_json = json.dumps({
        "message": "tighten NVDA cap to 12%",
        "intent": {"tier": "medium", "rationale": "user-asked"},
    })
    sess.commit()

    def _boom(**kw):
        raise RuntimeError("synthesizer down")

    monkeypatch.setattr(workers, "_run_phase_3_synthesizer", _boom)

    workers._medium_worker(
        session=sess, user_id="ariel", decision_run=run, guidance="x",
    )

    sess.refresh(run)
    assert run.status == "failed"
    notes = json.loads(run.notes_json)
    assert notes["message"] == "tighten NVDA cap to 12%"
    assert notes["intent"]["tier"] == "medium"
    assert notes["error"] == "synthesizer down"


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
