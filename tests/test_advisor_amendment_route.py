"""Tests for the amendment surface on POST /api/advisor/turn (Wave 4).

Adapts the spec's `_run_advisor_turn` monkeypatch target to the real
seam in ``argosy.api.routes.advisor.post_turn``: a
``set_advisor_agent_factory(...)`` hook that injects a fake
``AdvisorAgent`` whose ``_call_model`` returns a canned JSON payload.
The canned payload includes the new ``amendment`` field (Wave 4).
"""

from __future__ import annotations

import json

from argosy.agents.advisor import AdvisorAgent
from argosy.agents.base import ModelCall
from argosy.api.routes.advisor import (
    reset_advisor_agent_factory,
    set_advisor_agent_factory,
)
from argosy.state.models import DecisionRun, PlanVersion, User


def _seed_user_with_current(client_with_db) -> None:
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
        sess.add(PlanVersion(
            user_id="ariel", role="current", version_label="x",
            raw_markdown="",
            horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
            horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x","deltas_from_prior":[]}',
            horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"no_change","posture":"x"}',
        ))
        sess.commit()
    finally:
        sess.close()


def _canned_with_amendment(amendment: dict | None) -> dict:
    """Build a minimal AdvisorTurnOutput JSON payload, optionally with amendment."""
    base = {
        "stage": "stage_1",
        "question_for_user": "ack.",
        "context_updates": [],
        "stage_complete": False,
        "next_stage": None,
        "confidence": "MEDIUM",
        "cited_sources": [],
        "notes_for_orchestrator": "",
        "mode": "user_driven",
    }
    if amendment is not None:
        base["amendment"] = amendment
    return base


def _agent_factory_returning(canned: dict):
    """Factory matching ``set_advisor_agent_factory`` shape that returns
    an AdvisorAgent whose ``_call_model`` emits the canned dict as JSON."""
    def _make(user_id: str):
        class _StubAgent(AdvisorAgent):
            async def _call_model(self, *, system: str, user: str) -> ModelCall:
                return ModelCall(
                    text=json.dumps(canned),
                    tokens_in=10,
                    tokens_out=10,
                    model=self.model,
                )

        return _StubAgent(user_id=user_id)

    return _make


def test_turn_with_small_amendment_applies_inline(client_with_db, monkeypatch):
    """An advisor turn that emits tier=small + tighten + delta returns status=applied."""
    _seed_user_with_current(client_with_db)

    delta_dict = {
        "item_kind": "target",
        "item_id": "medium.targets.nvda",
        "horizon": "medium",
        "change_kind": "modified",
        "summary": "tighten NVDA cap",
        "prior": {"value": 0.15, "kind": "cap"},
        "proposed": {"value": 0.12, "kind": "cap"},
        "rationale": "user asked",
        "cited_sources": [],
        "accepted": False,
        "user_edited": False,
        "user_edit_note": None,
    }
    amendment = {
        "tier": "small",
        "direction": "tighten",
        "proposed_delta": delta_dict,
        "rationale": "tighten cap as requested",
        "requires_confirmation": False,
    }
    canned = _canned_with_amendment(amendment)
    set_advisor_agent_factory(_agent_factory_returning(canned))
    try:
        r = client_with_db.post(
            "/api/advisor/turn",
            json={"user_id": "ariel", "last_user_message": "tighten NVDA cap to 12%"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["amendment"] is not None
        assert body["amendment"]["status"] == "applied"
        assert body["amendment"]["tier"] == "small"
    finally:
        reset_advisor_agent_factory()


def test_turn_with_medium_amendment_returns_running(client_with_db, monkeypatch):
    """An advisor turn that emits tier=medium opens a DecisionRun + returns running."""
    from argosy.orchestrator.flows.plan_amendment import dispatcher as disp_mod

    _seed_user_with_current(client_with_db)

    amendment = {
        "tier": "medium",
        "direction": None,
        "proposed_delta": None,
        "rationale": "shift toward growth",
        "requires_confirmation": False,
    }
    canned = _canned_with_amendment(amendment)
    set_advisor_agent_factory(_agent_factory_returning(canned))

    spawned: list[dict] = []
    monkeypatch.setattr(disp_mod, "_spawn_worker", lambda **kw: spawned.append(kw))

    try:
        r = client_with_db.post(
            "/api/advisor/turn",
            json={"user_id": "ariel", "last_user_message": "shift toward growth"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["amendment"] is not None
        assert body["amendment"]["tier"] == "medium"
        assert body["amendment"]["status"] == "running"
        assert body["amendment"]["eta_seconds"] == 30
        assert len(spawned) == 1
    finally:
        reset_advisor_agent_factory()


def test_post_amendment_cancel_flips_status(client_with_db):
    """POST /api/advisor/amendment/{id}/cancel flips a running run to cancelled."""
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        run = DecisionRun(
            user_id="ariel", ticker="(plan)", tier="medium",
            decision_kind="plan_amendment_chat", status="running",
        )
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        run_id = run.id
    finally:
        sess.close()

    r = client_with_db.post(
        f"/api/advisor/amendment/{run_id}/cancel?user_id=ariel"
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"

    sess = client_with_db.app.state.session_factory()
    try:
        run = sess.get(DecisionRun, run_id)
        assert run.status == "cancelled"
        assert run.finished_at is not None
    finally:
        sess.close()


def test_post_amendment_cancel_404_for_unknown_run(client_with_db):
    r = client_with_db.post(
        "/api/advisor/amendment/9999/cancel?user_id=ariel"
    )
    assert r.status_code == 404


def _agent_factory_capturing_system(captured: dict):
    """Factory that records the assembled system prompt the agent sees.

    Wraps an AdvisorAgent stub so we can assert whether the AMENDMENT
    INTENT DETECTION block was injected. The prompt is captured in
    ``_call_model`` (which receives the full ``system`` string the
    framework would send to the LLM), so we test the actual production
    path: build_prompt → run → _call_model.
    """
    canned = _canned_with_amendment(None)

    def _make(user_id: str):
        class _Stub(AdvisorAgent):
            async def _call_model(self, *, system: str, user: str) -> ModelCall:
                captured["system"] = system
                captured["user"] = user
                return ModelCall(
                    text=json.dumps(canned),
                    tokens_in=1,
                    tokens_out=1,
                    model=self.model,
                )

        return _Stub(user_id=user_id)

    return _make


def test_turn_injects_amendment_block_when_user_has_current_plan(client_with_db):
    """The /turn route MUST query for a role='current' PlanVersion and pass
    has_current_plan=True so the AMENDMENT INTENT DETECTION block is in the
    system prompt the LLM actually sees.

    Without this wiring the entire amendment dispatch path is dead code:
    build_prompt's amendment block is gated on has_current_plan and the
    LLM never emits an `amendment` field.
    """
    _seed_user_with_current(client_with_db)

    captured: dict = {}
    set_advisor_agent_factory(_agent_factory_capturing_system(captured))
    try:
        r = client_with_db.post(
            "/api/advisor/turn",
            json={"user_id": "ariel", "last_user_message": "tighten NVDA"},
        )
        assert r.status_code == 200, r.text
        assert "AMENDMENT INTENT DETECTION" in captured.get("system", ""), (
            "amendment block must be in the system prompt when the user has "
            "a current plan; otherwise the dispatcher path is dead code."
        )
    finally:
        reset_advisor_agent_factory()


def test_turn_omits_amendment_block_when_user_has_no_current_plan(client_with_db):
    """User without a current PlanVersion gets the legacy (intake-only)
    prompt — no amendment block, since there's nothing to amend yet."""
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        sess.commit()
    finally:
        sess.close()

    captured: dict = {}
    set_advisor_agent_factory(_agent_factory_capturing_system(captured))
    try:
        r = client_with_db.post(
            "/api/advisor/turn",
            json={"user_id": "ariel", "last_user_message": "hi"},
        )
        assert r.status_code == 200, r.text
        assert "AMENDMENT INTENT DETECTION" not in captured.get("system", "")
    finally:
        reset_advisor_agent_factory()


def test_post_amendment_cancel_409_for_already_finished(client_with_db):
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        run = DecisionRun(
            user_id="ariel", ticker="(plan)", tier="medium",
            decision_kind="plan_amendment_chat", status="completed",
        )
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        run_id = run.id
    finally:
        sess.close()

    r = client_with_db.post(
        f"/api/advisor/amendment/{run_id}/cancel?user_id=ariel"
    )
    assert r.status_code == 409
