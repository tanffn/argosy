"""Tests for plan_amendment dispatcher (Wave 4)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import DecisionRun, PlanVersion, User


def _make_delta():
    from argosy.agents.plan_synthesizer_types import Delta
    return Delta(
        item_kind="target", item_id="medium.targets.nvda", horizon="medium",
        change_kind="modified", summary="NVDA cap 15% -> 12%",
        prior={"value": 0.15}, proposed={"value": 0.12},
        rationale="user-initiated tightening",
    )


def _make_intent(**kw):
    from argosy.agents.advisor_amendment_types import AmendmentIntent
    base = dict(tier="medium", rationale="x")
    base.update(kw)
    return AmendmentIntent(**base)


@pytest.fixture
def session_with_current(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.add(PlanVersion(
        user_id="ariel", role="baseline", version_label="x", raw_markdown="# Plan",
        distillate_rendered="# Plan distillate",
    ))
    pv = PlanVersion(
        user_id="ariel", role="draft", version_label="prior-draft",
        raw_markdown="",
        horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
        horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x","deltas_from_prior":[]}',
        horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"no_change","posture":"x"}',
    )
    s.add(pv)
    s.commit()
    s.refresh(pv)
    yield s, pv
    s.close()


def test_run_small_appends_delta_to_existing_draft(session_with_current):
    from argosy.orchestrator.flows.plan_amendment.dispatcher import run_small

    sess, pv = session_with_current
    intent_with_delta = _make_intent(tier="small", direction="tighten", proposed_delta=_make_delta())

    result = run_small(sess, user_id="ariel", message="tighten NVDA", intent=intent_with_delta)

    assert result.tier == "small"
    assert result.status == "applied"
    assert result.draft_id == pv.id
    sess.refresh(pv)
    import json
    med = json.loads(pv.horizon_medium_json)
    item_ids = [d["item_id"] for d in med["deltas_from_prior"]]
    assert "medium.targets.nvda" in item_ids
    delta = next(d for d in med["deltas_from_prior"] if d["item_id"] == "medium.targets.nvda")
    assert delta["accepted"] is True
    assert delta["user_edited"] is True


def test_run_small_creates_decision_run_row(session_with_current):
    from argosy.orchestrator.flows.plan_amendment.dispatcher import run_small

    sess, _pv = session_with_current
    intent = _make_intent(tier="small", direction="tighten", proposed_delta=_make_delta())

    result = run_small(sess, user_id="ariel", message="tighten NVDA", intent=intent)

    run = sess.get(DecisionRun, result.decision_run_id)
    assert run is not None
    assert run.decision_kind == "plan_amendment_chat"
    assert run.tier == "small"
    assert run.status == "completed"


def test_dispatch_async_blocks_when_amendment_already_running(session_with_current, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment.dispatcher import dispatch_async

    sess, _pv = session_with_current
    # Pre-existing running amendment.
    sess.add(DecisionRun(
        user_id="ariel", ticker="(plan)", tier="medium",
        decision_kind="plan_amendment_chat", status="running",
    ))
    sess.commit()

    intent = _make_intent(tier="medium")

    # Worker should NOT be dispatched.
    spawned = []
    monkeypatch.setattr(
        "argosy.orchestrator.flows.plan_amendment.dispatcher._spawn_worker",
        lambda *a, **kw: spawned.append(kw),
    )

    result = dispatch_async(
        sess, user_id="ariel", message="x", tier="medium", intent=intent,
    )

    assert result.status == "needs_confirmation"
    assert spawned == []


def test_dispatch_async_returns_running_when_no_conflict(session_with_current, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment.dispatcher import dispatch_async

    sess, _pv = session_with_current
    spawned = []
    monkeypatch.setattr(
        "argosy.orchestrator.flows.plan_amendment.dispatcher._spawn_worker",
        lambda **kw: spawned.append(kw),
    )

    intent = _make_intent(tier="medium")
    result = dispatch_async(
        sess, user_id="ariel", message="shift growth", tier="medium", intent=intent,
    )

    assert result.status == "running"
    assert result.tier == "medium"
    assert result.eta_seconds == 30
    assert len(spawned) == 1
    run = sess.get(DecisionRun, result.decision_run_id)
    assert run.status == "running"
    assert run.tier == "medium"


def test_dispatch_async_large_eta_is_900s(session_with_current, monkeypatch):
    from argosy.orchestrator.flows.plan_amendment.dispatcher import dispatch_async

    sess, _pv = session_with_current
    monkeypatch.setattr(
        "argosy.orchestrator.flows.plan_amendment.dispatcher._spawn_worker",
        lambda **kw: None,
    )

    intent = _make_intent(tier="large")
    result = dispatch_async(
        sess, user_id="ariel", message="re-evaluate", tier="large", intent=intent,
    )

    assert result.tier == "large"
    assert result.eta_seconds == 900


def test_dispatch_async_cancel_existing_returns_cancelled_existing(
    session_with_current, monkeypatch,
):
    """When cancel_existing=True and a prior running amendment exists, the
    dispatcher cancels the prior, opens a new run, and returns
    status='cancelled_existing' so the UI can confirm both transitions
    happened atomically (spec §7.1)."""
    from argosy.orchestrator.flows.plan_amendment.dispatcher import dispatch_async

    sess, _pv = session_with_current
    sess.add(DecisionRun(
        user_id="ariel", ticker="(plan)", tier="medium",
        decision_kind="plan_amendment_chat", status="running",
    ))
    sess.commit()

    spawned = []
    monkeypatch.setattr(
        "argosy.orchestrator.flows.plan_amendment.dispatcher._spawn_worker",
        lambda **kw: spawned.append(kw),
    )

    intent = _make_intent(tier="medium", cancel_existing=True)
    result = dispatch_async(
        sess, user_id="ariel", message="restart please", tier="medium",
        intent=intent, cancel_existing=True,
    )

    assert result.status == "cancelled_existing"
    assert result.tier == "medium"
    assert result.eta_seconds == 30
    assert len(spawned) == 1

    # Prior run should be cancelled; new run should be running.
    runs = (
        sess.query(DecisionRun)
        .filter_by(user_id="ariel", decision_kind="plan_amendment_chat")
        .order_by(DecisionRun.id)
        .all()
    )
    assert len(runs) == 2
    assert runs[0].status == "cancelled"
    assert runs[1].status == "running"


def test_run_small_no_orphan_decision_run_when_no_current_plan(
    alembic_engine_at_head,
):
    """If user has neither pending draft nor current plan, run_small must
    raise BEFORE writing a `running` DecisionRun row — otherwise the
    partial unique index permanently jams future amendment attempts.
    """
    from sqlalchemy.orm import sessionmaker

    from argosy.orchestrator.flows.plan_amendment.dispatcher import run_small

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    sess = SessionLocal()
    try:
        sess.add(User(id="ariel", plan="free"))
        sess.commit()

        intent = _make_intent(
            tier="small", direction="tighten", proposed_delta=_make_delta(),
        )

        with pytest.raises(RuntimeError, match="no current plan"):
            run_small(sess, user_id="ariel", message="x", intent=intent)

        # No running row should have been left behind to jam the
        # partial unique index for this user.
        runs = (
            sess.query(DecisionRun)
            .filter_by(user_id="ariel", decision_kind="plan_amendment_chat")
            .all()
        )
        assert runs == [], (
            f"orphan DecisionRun rows leaked: {[r.status for r in runs]}"
        )
    finally:
        sess.close()


def test_run_small_rejects_loosening_numbers(session_with_current):
    """Even if intent claims direction=tighten, if the numbers loosen we refuse."""
    from argosy.orchestrator.flows.plan_amendment.dispatcher import run_small
    from argosy.agents.advisor_amendment_types import AmendmentIntent
    from argosy.agents.plan_synthesizer_types import Delta

    sess, _pv = session_with_current

    # Spec says cap "tightening", but proposed > prior — actually loosening.
    bad_delta = Delta(
        item_kind="target", item_id="medium.targets.nvda", horizon="medium",
        change_kind="modified", summary="x",
        prior={"value": 0.15, "kind": "cap"},
        proposed={"value": 0.18, "kind": "cap"},
        rationale="claims tightening but numbers loosen",
    )
    intent = AmendmentIntent(
        tier="small", direction="tighten", proposed_delta=bad_delta, rationale="x",
    )

    import pytest
    with pytest.raises(ValueError, match="tightening"):
        run_small(sess, user_id="ariel", message="x", intent=intent)
