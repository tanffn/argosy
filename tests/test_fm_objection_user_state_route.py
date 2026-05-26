"""Per-FM-objection user-state endpoints + start-new-round flow.

Covers the four new endpoints introduced for the agree/disagree flow:

  GET  /api/plan/draft/objections/state         — read map
  PUT  /api/plan/draft/objections/state         — upsert one row
  POST /api/plan/draft/objections/start-new-round — compose guidance + dispatch synthesis

The start-new-round path is exercised end-to-end with a monkey-patched
``run_synthesis`` so we can capture the composed guidance string and
assert it carries AGREED / DISAGREED / DEFERRED buckets with the user's
counter-position threaded through.
"""

from __future__ import annotations

import json

import pytest

from argosy.state.models import (
    AgentReport,
    FMObjectionUserState,
    PlanVersion,
    User,
)


# ----------------------------------------------------------------------
# Fixture — baseline + draft + a fund_manager agent_report with three
# objections (1 RED, 1 AMBER, 1 YELLOW). Mirrors the structure used by
# tests/test_plan_draft_api.py::test_get_draft_objections_parses_fm_response
# so the per-objection-index math is comparable.
# ----------------------------------------------------------------------


@pytest.fixture
def app_with_objections(client_with_db):
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        sess.add(
            PlanVersion(
                user_id="ariel", role="baseline", raw_markdown="# Plan",
            )
        )
        draft = PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="synth-test",
            raw_markdown="",
            decision_run_id=1,
        )
        sess.add(draft)
        sess.commit()
        sess.refresh(draft)
        # FM agent_report with three reasons -> three objections,
        # indices 0..2 in the same order returned by the parse path.
        sess.add(
            AgentReport(
                user_id="ariel",
                agent_role="fund_manager",
                decision_id="plan-synth-1",
                response_text=json.dumps(
                    {
                        "approved": False,
                        "reasons": [
                            "TIME-CRITICAL HARD CONSTRAINT VIOLATION — section 102 missed",
                            "MISSING DRAWDOWN STOP — no downside trigger defined",
                            "MINOR THEME — small thing",
                        ],
                        "cited_sources": [],
                    }
                ),
                model="claude-opus-4-7",
            )
        )
        sess.commit()
        draft_id = draft.id
    finally:
        sess.close()
    return client_with_db, draft_id


# ----------------------------------------------------------------------
# (a) GET returns empty when no state yet.
# ----------------------------------------------------------------------


def test_get_state_empty_when_no_rows(app_with_objections):
    tc, draft_id = app_with_objections
    r = tc.get(
        f"/api/plan/draft/objections/state?user_id=ariel"
        f"&plan_version_id={draft_id}"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["states"] == {}
    assert body["plan_version_id"] == draft_id


# ----------------------------------------------------------------------
# (b) PUT upserts and validates DISAGREE requires counter_position.
# ----------------------------------------------------------------------


def test_put_state_upserts_agree(app_with_objections):
    tc, draft_id = app_with_objections
    r = tc.put(
        "/api/plan/draft/objections/state",
        json={
            "user_id": "ariel",
            "plan_version_id": draft_id,
            "objection_index": 0,
            "stance": "AGREE",
            "topic": "TIME-CRITICAL HARD CONSTRAINT VIOLATION",
            "detail": "section 102 missed",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["stance"] == "AGREE"

    # Idempotent update — flip to DEFER on the same index.
    r = tc.put(
        "/api/plan/draft/objections/state",
        json={
            "user_id": "ariel",
            "plan_version_id": draft_id,
            "objection_index": 0,
            "stance": "DEFER",
        },
    )
    assert r.status_code == 200

    sess = tc.app.state.session_factory()
    try:
        rows = sess.query(FMObjectionUserState).filter_by(
            user_id="ariel", plan_version_id=draft_id, objection_index=0,
        ).all()
        # Idempotent — only one row, updated in place.
        assert len(rows) == 1
        assert rows[0].stance == "DEFER"
    finally:
        sess.close()


def test_put_disagree_requires_counter_position(app_with_objections):
    tc, draft_id = app_with_objections
    # No counter_position
    r = tc.put(
        "/api/plan/draft/objections/state",
        json={
            "user_id": "ariel",
            "plan_version_id": draft_id,
            "objection_index": 1,
            "stance": "DISAGREE",
        },
    )
    assert r.status_code == 400, r.text
    assert "counter_position" in r.json()["detail"]

    # Whitespace-only counter_position — still rejected.
    r = tc.put(
        "/api/plan/draft/objections/state",
        json={
            "user_id": "ariel",
            "plan_version_id": draft_id,
            "objection_index": 1,
            "stance": "DISAGREE",
            "counter_position": "   ",
        },
    )
    assert r.status_code == 400, r.text

    # With a real counter_position — accepted.
    r = tc.put(
        "/api/plan/draft/objections/state",
        json={
            "user_id": "ariel",
            "plan_version_id": draft_id,
            "objection_index": 1,
            "stance": "DISAGREE",
            "counter_position": "I want a 12% drawdown trigger, not 8%.",
        },
    )
    assert r.status_code == 200, r.text


def test_put_state_403_when_plan_owned_by_other_user(app_with_objections):
    tc, draft_id = app_with_objections
    r = tc.put(
        "/api/plan/draft/objections/state",
        json={
            "user_id": "intruder",
            "plan_version_id": draft_id,
            "objection_index": 0,
            "stance": "AGREE",
        },
    )
    assert r.status_code == 403, r.text


def test_put_state_400_for_unknown_stance(app_with_objections):
    tc, draft_id = app_with_objections
    r = tc.put(
        "/api/plan/draft/objections/state",
        json={
            "user_id": "ariel",
            "plan_version_id": draft_id,
            "objection_index": 0,
            "stance": "MAYBE",
        },
    )
    assert r.status_code == 400, r.text


def test_get_state_returns_rows_after_upserts(app_with_objections):
    tc, draft_id = app_with_objections
    tc.put(
        "/api/plan/draft/objections/state",
        json={
            "user_id": "ariel",
            "plan_version_id": draft_id,
            "objection_index": 0,
            "stance": "AGREE",
        },
    )
    tc.put(
        "/api/plan/draft/objections/state",
        json={
            "user_id": "ariel",
            "plan_version_id": draft_id,
            "objection_index": 1,
            "stance": "DISAGREE",
            "counter_position": "I prefer a 12% drawdown threshold.",
        },
    )
    r = tc.get(
        f"/api/plan/draft/objections/state?user_id=ariel"
        f"&plan_version_id={draft_id}"
    )
    assert r.status_code == 200
    states = r.json()["states"]
    assert states["0"]["stance"] == "AGREE"
    assert states["1"]["stance"] == "DISAGREE"
    assert "12%" in states["1"]["counter_position"]


# ----------------------------------------------------------------------
# (c) start-new-round dispatches synthesis with composed guidance.
# ----------------------------------------------------------------------


def test_start_new_round_composes_guidance_and_dispatches(
    app_with_objections, monkeypatch,
):
    """Mock run_synthesis to capture the composed guidance string.
    Assert it carries AGREED / DISAGREED / DEFERRED buckets with the
    user's counter-position threaded through.
    """
    from argosy.orchestrator.flows import plan_synthesis as flow

    tc, draft_id = app_with_objections

    # Set per-objection state: AGREE on idx 0, DISAGREE on idx 1, idx 2 left as DEFER.
    tc.put(
        "/api/plan/draft/objections/state",
        json={
            "user_id": "ariel",
            "plan_version_id": draft_id,
            "objection_index": 0,
            "stance": "AGREE",
        },
    )
    tc.put(
        "/api/plan/draft/objections/state",
        json={
            "user_id": "ariel",
            "plan_version_id": draft_id,
            "objection_index": 1,
            "stance": "DISAGREE",
            "counter_position": "I want a 12% drawdown trigger, not 8%.",
        },
    )

    captured: dict = {}

    def _fake_run(
        session,
        *,
        user_id,
        trigger,
        guidance="",
        existing_decision_run_id=None,
        resume_from_phase=1,
    ):
        captured["user_id"] = user_id
        captured["trigger"] = trigger
        captured["guidance"] = guidance
        captured["existing_decision_run_id"] = existing_decision_run_id

        class _R:
            decision_run_id = existing_decision_run_id or 999
            draft_id = 1

        return _R()

    monkeypatch.setattr(flow, "run_synthesis", _fake_run)

    r = tc.post(
        f"/api/plan/draft/objections/start-new-round?user_id=ariel"
        f"&plan_version_id={draft_id}"
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["n_agreed"] == 1
    assert body["n_disagreed"] == 1
    assert body["n_deferred"] == 1
    assert isinstance(body["decision_run_id"], int)
    assert body["decision_audit_token"] == f"plan-synth-{body['decision_run_id']}"

    # Background task drained — the patched run_synthesis was called.
    assert captured["user_id"] == "ariel"
    assert captured["trigger"] == "check_in"
    g = captured["guidance"]
    assert "AGREED OBJECTIONS" in g
    assert "DISAGREED OBJECTIONS" in g
    assert "DEFERRED OBJECTIONS" in g
    # The AGREED bucket contains the RED objection (idx 0).
    assert "section 102" in g
    # The DISAGREED bucket carries the user's counter-position verbatim.
    assert "12% drawdown trigger" in g
    assert "USER COUNTER-POSITION" in g
    # The DEFERRED bucket carries the YELLOW objection (idx 2).
    assert "MINOR THEME" in g


# ----------------------------------------------------------------------
# (d) start-new-round refuses with 400 when every objection is DEFER.
# ----------------------------------------------------------------------


def test_start_new_round_refuses_when_all_defer(app_with_objections):
    tc, draft_id = app_with_objections
    # Set one row with stance=DEFER explicitly (counts toward DEFER bucket).
    tc.put(
        "/api/plan/draft/objections/state",
        json={
            "user_id": "ariel",
            "plan_version_id": draft_id,
            "objection_index": 0,
            "stance": "DEFER",
        },
    )
    r = tc.post(
        f"/api/plan/draft/objections/start-new-round?user_id=ariel"
        f"&plan_version_id={draft_id}"
    )
    assert r.status_code == 400, r.text
    assert "DEFER" in r.json()["detail"]


def test_start_new_round_refuses_when_no_state_rows_at_all(
    app_with_objections,
):
    tc, draft_id = app_with_objections
    r = tc.post(
        f"/api/plan/draft/objections/start-new-round?user_id=ariel"
        f"&plan_version_id={draft_id}"
    )
    assert r.status_code == 400, r.text


def test_start_new_round_404_when_plan_not_for_user(app_with_objections):
    tc, draft_id = app_with_objections
    r = tc.post(
        f"/api/plan/draft/objections/start-new-round?user_id=intruder"
        f"&plan_version_id={draft_id}"
    )
    # 403 — plan_version exists but belongs to ariel, not intruder.
    assert r.status_code == 403, r.text


# ----------------------------------------------------------------------
# (e) GET map response carries plan_version_id back so the UI doesn't
# need a separate /api/plan/draft round-trip just to learn the id.
# ----------------------------------------------------------------------


def test_get_state_response_carries_plan_version_id(app_with_objections):
    tc, draft_id = app_with_objections
    r = tc.get(
        f"/api/plan/draft/objections/state?user_id=ariel"
        f"&plan_version_id={draft_id}"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_version_id"] == draft_id
