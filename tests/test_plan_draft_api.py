"""Draft lifecycle endpoints — see spec §7.6."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


@pytest.fixture
def app_with_draft(client_with_db):
    """Insert a baseline + a draft for user ariel."""
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
        sess.add(PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="synth-2026-05",
            raw_markdown="",
            horizon_long_md="# Long",
            horizon_medium_md="# Medium",
            horizon_short_md="# Short",
            horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
            horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"minor_revision","posture":"x"}',
            horizon_short_json='{"horizon":"short","freshness_expected":"monthly","status":"major_revision","posture":"x"}',
        ))
        sess.commit()
    finally:
        sess.close()
    return client_with_db


def test_get_draft_returns_pending(app_with_draft):
    r = app_with_draft.get("/api/plan/draft?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["plan_version_id"] is not None
    assert body["horizon_long"]["horizon"] == "long"
    assert body["horizon_medium"]["status"] == "minor_revision"


def test_get_draft_404_when_absent(client_with_db):
    r = client_with_db.get("/api/plan/draft?user_id=newcomer")
    assert r.status_code == 404


def test_get_draft_synthesis_health_present_when_decision_run_id(app_with_draft):
    """T0.7: when a pending draft carries a decision_run_id pointing at a
    synthesis run, the /api/plan/draft response includes a populated
    ``synthesis_health`` block derived from the FM-rooted agent-tree
    builder's status_summary.
    """
    from datetime import datetime, timezone

    from argosy.state.models import (
        AgentReport,
        DecisionPhase,
        DecisionRun,
        PlanVersion,
    )

    sess = app_with_draft.app.state.session_factory()
    try:
        now = datetime.now(timezone.utc)
        run = DecisionRun(
            user_id="ariel",
            ticker="(plan)",
            tier=None,
            decision_kind="plan_revision",
            status="completed",
            started_at=now,
            finished_at=now,
        )
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        rid = run.id
        decision_id_str = f"plan-synth-{rid}"

        # Wire the draft to the synthesis run.
        draft = sess.query(PlanVersion).filter_by(
            user_id="ariel", role="draft"
        ).one()
        draft.decision_run_id = rid

        # Seed the 18 agent_reports the builder expects, all OK confidence.
        analyst_roles = [
            "concentration", "fx", "fundamentals", "news",
            "sentiment", "technical", "macro", "tax",
            "household_budget", "plan_critique",
        ]
        for role in analyst_roles:
            sess.add(AgentReport(
                user_id="ariel", agent_role=role,
                decision_id=decision_id_str, response_text="ok",
                confidence="MEDIUM", model="claude-sonnet-4-6",
                tokens_in=10, tokens_out=20, cost_usd=0.001,
            ))
        # Three rows each for bull/bear/researcher_facilitator (one per
        # horizon — the builder pops 3× of each), plus the single
        # plan_synthesizer row. Without three of each, the missing slots
        # show up as "skipped" and inflate agents_failed.
        for _ in range(3):
            for role in ("bull_researcher", "bear_researcher",
                         "researcher_facilitator"):
                sess.add(AgentReport(
                    user_id="ariel", agent_role=role,
                    decision_id=decision_id_str, response_text="ok",
                    confidence="MEDIUM", model="claude-opus-4-7",
                    tokens_in=10, tokens_out=20, cost_usd=0.001,
                ))
        sess.add(AgentReport(
            user_id="ariel", agent_role="plan_synthesizer",
            decision_id=decision_id_str, response_text="ok",
            confidence="MEDIUM", model="claude-opus-4-7",
            tokens_in=10, tokens_out=20, cost_usd=0.001,
        ))
        for _ in range(3):
            sess.add(AgentReport(
                user_id="ariel", agent_role="risk_officer",
                decision_id=decision_id_str, response_text="ok",
                confidence="MEDIUM", model="claude-opus-4-7",
                tokens_in=10, tokens_out=20, cost_usd=0.001,
            ))
        sess.add(AgentReport(
            user_id="ariel", agent_role="risk_facilitator",
            decision_id=decision_id_str, response_text="ok",
            confidence="MEDIUM", model="claude-opus-4-7",
            tokens_in=10, tokens_out=20, cost_usd=0.001,
        ))
        sess.add(AgentReport(
            user_id="ariel", agent_role="fund_manager",
            decision_id=decision_id_str, response_text="ok",
            confidence="MEDIUM", model="claude-opus-4-7",
            tokens_in=10, tokens_out=20, cost_usd=0.001,
        ))

        # Phase 1 row with two adapter outcomes (one ok, one http_error).
        phase_output = {
            "phase": 1,
            "adapter_outcomes": [
                {
                    "adapter_name": "finnhub",
                    "target": "NVDA",
                    "status": "ok",
                    "latency_ms": 100,
                    "payload_size_bytes": 1024,
                    "http_status_code": 200,
                    "error_text": None,
                },
                {
                    "adapter_name": "fred",
                    "target": "DGS10",
                    "status": "http_error",
                    "latency_ms": 5000,
                    "payload_size_bytes": 0,
                    "http_status_code": 503,
                    "error_text": "FRED down",
                },
            ],
        }
        sess.add(DecisionPhase(
            decision_run_id=rid, user_id="ariel", seq=1,
            kind="synthesis.phase_1",
            started_at=now, finished_at=now,
            participants_json="[]",
            phase_output_json=json.dumps(phase_output),
        ))
        sess.commit()
    finally:
        sess.close()

    r = app_with_draft.get("/api/plan/draft?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "synthesis_health" in body
    health = body["synthesis_health"]
    assert health is not None
    assert health["decision_run_id"] == rid
    # Every agent ran -> agents_failed = 0; agents_ok must be >= 1.
    assert health["agents_failed"] == 0
    assert health["agents_ok"] >= 1
    # One ok adapter + one http_error adapter were seeded.
    assert health["adapters_ok"] == 1
    assert health["adapters_failed"] == 1


def test_get_draft_synthesis_health_null_when_no_decision_run_id(app_with_draft):
    """T0.7: drafts without a decision_run_id return synthesis_health=None
    rather than raising. The fixture's draft has decision_run_id=None.
    """
    r = app_with_draft.get("/api/plan/draft?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "synthesis_health" in body
    assert body["synthesis_health"] is None


def test_get_draft_synthesis_health_null_when_builder_raises(app_with_draft):
    """T0.7: when build_agent_tree raises ValueError (e.g. decision_run_id
    points at a non-existent run, or a non-synthesis run), the route
    degrades to ``synthesis_health=None`` rather than crashing.
    """
    from argosy.state.models import PlanVersion

    sess = app_with_draft.app.state.session_factory()
    try:
        draft = sess.query(PlanVersion).filter_by(
            user_id="ariel", role="draft"
        ).one()
        # Wire a decision_run_id pointing at no real run.
        draft.decision_run_id = 999_999
        sess.commit()
    finally:
        sess.close()

    r = app_with_draft.get("/api/plan/draft?user_id=ariel")
    assert r.status_code == 200, r.text
    assert r.json()["synthesis_health"] is None


def test_get_draft_objections_parses_fm_response(app_with_draft):
    """FM response_text JSON is parsed into severity-tagged objections."""
    from argosy.state.models import AgentReport, PlanVersion

    # Wire a decision_run_id + a fund_manager agent_report to the draft.
    sess = app_with_draft.app.state.session_factory()
    try:
        draft = sess.query(PlanVersion).filter_by(
            user_id="ariel", role="draft"
        ).one()
        draft.decision_run_id = 1
        sess.add(AgentReport(
            user_id="ariel",
            agent_role="fund_manager",
            decision_id="plan-synth-1",
            response_text=json.dumps({
                "approved": False,
                "reasons": [
                    "TIME-CRITICAL HARD CONSTRAINT VIOLATION — section 102 missed",
                    "MISSING DRAWDOWN STOP — no downside trigger defined",
                    "MINOR THEME — small thing",
                ],
                "cited_sources": ["agent_report:TaxAnalystAgent", "user_context.x"],
            }),
            model="claude-opus-4-7",
        ))
        sess.commit()
    finally:
        sess.close()

    r = app_with_draft.get("/api/plan/draft/objections?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approved"] is False
    assert len(body["objections"]) == 3
    sev = {o["topic"]: o["severity"] for o in body["objections"]}
    # "TIME-CRITICAL" + "HARD CONSTRAINT VIOLATION" + "section 102" all RED triggers
    assert sev["TIME-CRITICAL HARD CONSTRAINT VIOLATION"] == "RED"
    # "MISSING" triggers AMBER
    assert sev["MISSING DRAWDOWN STOP"] == "AMBER"
    # No keyword hit -> default YELLOW
    assert sev["MINOR THEME"] == "YELLOW"
    assert "agent_report:TaxAnalystAgent" in body["cited_sources"]
    assert body["decision_run_id"] == 1


def test_post_delta_reject_stamps_user_edit_note(app_with_draft):
    """Per-delta reject writes REJECTED prefix + flips user_edited."""
    from argosy.state.models import PlanVersion

    sess = app_with_draft.app.state.session_factory()
    try:
        draft = sess.query(PlanVersion).filter_by(
            user_id="ariel", role="draft"
        ).one()
        draft.horizon_long_json = json.dumps({
            "horizon": "long",
            "freshness_expected": "annual",
            "status": "no_change",
            "posture": "test",
            "deltas_from_prior": [{
                "item_kind": "target",
                "item_id": "long.targets.x",
                "horizon": "long",
                "change_kind": "added",
                "summary": "x",
                "rationale": "",
                "cited_sources": [],
                "accepted": False,
                "user_edited": False,
                "user_edit_note": None,
            }],
        })
        sess.commit()
        draft_id = draft.id
    finally:
        sess.close()

    r = app_with_draft.post(
        f"/api/plan/draft/{draft_id}/items/long.targets.x/reject?user_id=ariel",
        json={"reason": "doesn't fit my timeline"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"

    # Re-fetch and verify persisted state
    r2 = app_with_draft.get("/api/plan/draft?user_id=ariel")
    delta = r2.json()["horizon_long"]["deltas_from_prior"][0]
    assert delta["accepted"] is False
    assert delta["user_edited"] is True
    assert delta["user_edit_note"].startswith("REJECTED:")
    assert "timeline" in delta["user_edit_note"]


def test_post_delta_pushback_accumulates_in_user_edit_note(
    app_with_draft, monkeypatch,
):
    """Multiple pushbacks append to user_edit_note rather than overwriting.

    T4.3 — disable the slim re-debate background dispatch so the test
    asserts only on the legacy user_edit_note persistence side-effect
    (the slim flow gets its own coverage in test_per_delta_pushback.py).
    """
    monkeypatch.setenv("ARGOSY_DISABLE_PER_DELTA_PUSHBACK_REDEBATE", "1")
    from argosy.state.models import PlanVersion

    sess = app_with_draft.app.state.session_factory()
    try:
        draft = sess.query(PlanVersion).filter_by(
            user_id="ariel", role="draft"
        ).one()
        draft.horizon_medium_json = json.dumps({
            "horizon": "medium",
            "freshness_expected": "quarterly",
            "status": "minor_revision",
            "posture": "test",
            "deltas_from_prior": [{
                "item_kind": "action",
                "item_id": "medium.actions.foo",
                "horizon": "medium",
                "change_kind": "modified",
                "summary": "foo",
                "rationale": "",
                "cited_sources": [],
                "accepted": False,
                "user_edited": False,
                "user_edit_note": None,
            }],
        })
        sess.commit()
        draft_id = draft.id
    finally:
        sess.close()

    # First pushback
    r1 = app_with_draft.post(
        f"/api/plan/draft/{draft_id}/items/medium.actions.foo/pushback?user_id=ariel",
        json={"feedback": "consider tax implications"},
    )
    assert r1.status_code == 200

    # Second pushback — should APPEND, not overwrite
    r2 = app_with_draft.post(
        f"/api/plan/draft/{draft_id}/items/medium.actions.foo/pushback?user_id=ariel",
        json={"feedback": "also check FX exposure"},
    )
    assert r2.status_code == 200

    r3 = app_with_draft.get("/api/plan/draft?user_id=ariel")
    delta = r3.json()["horizon_medium"]["deltas_from_prior"][0]
    note = delta["user_edit_note"]
    assert "PUSHBACK: consider tax" in note
    assert "PUSHBACK: also check FX" in note
    assert note.count("PUSHBACK:") == 2


def test_post_delta_pushback_400_when_feedback_empty(
    app_with_draft, monkeypatch,
):
    """Empty feedback rejected — pushback must carry actual user input."""
    monkeypatch.setenv("ARGOSY_DISABLE_PER_DELTA_PUSHBACK_REDEBATE", "1")
    from argosy.state.models import PlanVersion

    sess = app_with_draft.app.state.session_factory()
    try:
        draft = sess.query(PlanVersion).filter_by(
            user_id="ariel", role="draft"
        ).one()
        draft.horizon_short_json = json.dumps({
            "horizon": "short",
            "freshness_expected": "monthly",
            "status": "no_change",
            "posture": "x",
            "deltas_from_prior": [{
                "item_kind": "target",
                "item_id": "short.targets.y",
                "horizon": "short",
                "change_kind": "added",
                "summary": "y",
                "rationale": "",
                "cited_sources": [],
                "accepted": False,
                "user_edited": False,
                "user_edit_note": None,
            }],
        })
        sess.commit()
        draft_id = draft.id
    finally:
        sess.close()

    r = app_with_draft.post(
        f"/api/plan/draft/{draft_id}/items/short.targets.y/pushback?user_id=ariel",
        json={"feedback": "   "},
    )
    assert r.status_code == 400


def test_get_draft_objections_404_when_no_draft(client_with_db):
    r = client_with_db.get("/api/plan/draft/objections?user_id=newcomer")
    assert r.status_code == 404


def test_get_draft_enriches_deltas_with_provenance_labels(app_with_draft):
    """Each delta gets a provenance_agent_labels list derived from citations."""
    from argosy.state.models import PlanVersion

    sess = app_with_draft.app.state.session_factory()
    try:
        draft = sess.query(PlanVersion).filter_by(
            user_id="ariel", role="draft"
        ).one()
        # Inject a horizon_long_json with a delta carrying mixed citation kinds.
        draft.horizon_long_json = json.dumps({
            "horizon": "long",
            "freshness_expected": "annual",
            "status": "major_revision",
            "posture": "test",
            "deltas_from_prior": [{
                "item_kind": "target",
                "item_id": "long.targets.nvda",
                "horizon": "long",
                "change_kind": "added",
                "summary": "NVDA cap",
                "rationale": "",
                "cited_sources": [
                    "agent_report:TaxAnalystAgent",
                    "fundamentals/NVDA",
                    "user_context.rsu_vest_schedule",
                    "decision_run:debate_outcome_long",
                ],
                "accepted": False,
                "user_edited": False,
                "user_edit_note": None,
            }],
        })
        sess.commit()
    finally:
        sess.close()

    r = app_with_draft.get("/api/plan/draft?user_id=ariel")
    assert r.status_code == 200
    deltas = r.json()["horizon_long"]["deltas_from_prior"]
    assert len(deltas) == 1
    labels = deltas[0]["provenance_agent_labels"]
    # Order-preserving dedup; we expect TaxAnalyst (from agent_report:),
    # FundamentalsAnalyst (from fundamentals/), user_context, Debate (long).
    assert labels == ["TaxAnalyst", "FundamentalsAnalyst", "user_context", "Debate (long)"]


def test_post_draft_accept_promotes_to_current(app_with_draft):
    r1 = app_with_draft.get("/api/plan/draft?user_id=ariel")
    draft_id = r1.json()["plan_version_id"]

    r2 = app_with_draft.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "accepted"
    assert body["new_current_id"] == draft_id

    # Inspect: draft is now role=current.
    sess = app_with_draft.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        assert pv.role == "current"
        assert pv.accepted_at is not None
        assert pv.accepted_by_user_id == "ariel"
    finally:
        sess.close()


def test_post_draft_accept_supersedes_prior_current(app_with_draft):
    # Insert a prior current first.
    sess = app_with_draft.app.state.session_factory()
    try:
        sess.add(PlanVersion(user_id="ariel", role="current", version_label="prior"))
        sess.commit()
        prior_id = sess.query(PlanVersion).filter_by(user_id="ariel", role="current").one().id
    finally:
        sess.close()

    r1 = app_with_draft.get("/api/plan/draft?user_id=ariel")
    draft_id = r1.json()["plan_version_id"]
    r2 = app_with_draft.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    assert r2.status_code == 200

    sess = app_with_draft.app.state.session_factory()
    try:
        prior = sess.get(PlanVersion, prior_id)
        assert prior.role == "superseded"
        assert prior.superseded_at is not None
    finally:
        sess.close()


def test_post_draft_reject_marks_superseded(app_with_draft):
    r1 = app_with_draft.get("/api/plan/draft?user_id=ariel")
    draft_id = r1.json()["plan_version_id"]

    r2 = app_with_draft.post(
        f"/api/plan/draft/{draft_id}/reject?user_id=ariel",
        json={"reason": "macro analyst was too cautious", "guidance": "weight aggressive risk"},
    )
    assert r2.status_code == 200

    sess = app_with_draft.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        assert pv.role == "superseded"
        assert pv.superseded_at is not None
    finally:
        sess.close()


def test_post_delta_accept_marks_item_accepted(app_with_draft):
    """Per-delta accept flips the `accepted` flag on a Delta within a horizon."""
    sess = app_with_draft.app.state.session_factory()
    try:
        # Inject a delta into the draft's horizon_medium_json.
        from argosy.state.queries import get_pending_draft
        pv = get_pending_draft(sess, "ariel")
        import json as _j
        med = _j.loads(pv.horizon_medium_json)
        med["deltas_from_prior"] = [{
            "item_kind": "target",
            "item_id": "medium.targets.nvda",
            "horizon": "medium",
            "change_kind": "modified",
            "summary": "NVDA target tightened 15% -> 12%",
            "prior": {"value": 0.15},
            "proposed": {"value": 0.12},
            "rationale": "macro analyst flagged DeepSeek + tariff overhang",
            "cited_sources": [],
            "accepted": False,
            "user_edited": False,
            "user_edit_note": None,
        }]
        pv.horizon_medium_json = _j.dumps(med)
        sess.commit()
        draft_id = pv.id
    finally:
        sess.close()

    r = app_with_draft.post(
        f"/api/plan/draft/{draft_id}/items/medium.targets.nvda/accept?user_id=ariel"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"

    sess = app_with_draft.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        med = json.loads(pv.horizon_medium_json)
        delta = next(d for d in med["deltas_from_prior"] if d["item_id"] == "medium.targets.nvda")
        assert delta["accepted"] is True
    finally:
        sess.close()


def test_patch_delta_user_edit_records_change(app_with_draft):
    sess = app_with_draft.app.state.session_factory()
    try:
        from argosy.state.queries import get_pending_draft
        pv = get_pending_draft(sess, "ariel")
        import json as _j
        med = _j.loads(pv.horizon_medium_json)
        med["deltas_from_prior"] = [{
            "item_kind": "target",
            "item_id": "medium.targets.nvda",
            "horizon": "medium",
            "change_kind": "modified",
            "summary": "...",
            "prior": {"value": 0.15},
            "proposed": {"value": 0.12},
            "rationale": "...",
            "cited_sources": [],
            "accepted": False,
            "user_edited": False,
            "user_edit_note": None,
        }]
        pv.horizon_medium_json = _j.dumps(med)
        sess.commit()
        draft_id = pv.id
    finally:
        sess.close()

    r = app_with_draft.patch(
        f"/api/plan/draft/{draft_id}/items/medium.targets.nvda?user_id=ariel",
        json={"proposed": {"value": 0.13}, "user_edit_note": "tighter, but 13%"},
    )
    assert r.status_code == 200, r.text

    sess = app_with_draft.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, draft_id)
        med = json.loads(pv.horizon_medium_json)
        delta = next(d for d in med["deltas_from_prior"] if d["item_id"] == "medium.targets.nvda")
        assert delta["proposed"]["value"] == 0.13
        assert delta["user_edited"] is True
        assert delta["user_edit_note"] == "tighter, but 13%"
    finally:
        sess.close()


def test_patch_delta_invalid_edit_returns_400(app_with_draft):
    """M3: patch_delta_edit must reject a patch that leaves the Delta in an invalid state.

    We store a delta whose item_kind is intentionally set to an invalid value
    (not one of the allowed Literals).  A user edit that is valid at the
    DeltaEditRequest level passes FastAPI body validation, but the route must
    re-validate the full resulting Delta and return 400 when it fails.
    """
    sess = app_with_draft.app.state.session_factory()
    try:
        from argosy.state.queries import get_pending_draft
        import json as _j
        pv = get_pending_draft(sess, "ariel")
        med = _j.loads(pv.horizon_medium_json)
        med["deltas_from_prior"] = [{
            # Intentionally corrupt: "bogus_kind" is not a valid item_kind.
            "item_kind": "bogus_kind",
            "item_id": "medium.targets.nvda",
            "horizon": "medium",
            "change_kind": "modified",
            "summary": "...",
            "prior": {"value": 0.15},
            "proposed": {"value": 0.12},
            "rationale": "...",
            "cited_sources": [],
            "accepted": False,
            "user_edited": False,
            "user_edit_note": None,
        }]
        pv.horizon_medium_json = _j.dumps(med)
        sess.commit()
        draft_id = pv.id
    finally:
        sess.close()

    # A valid DeltaEditRequest body — passes FastAPI body validation.
    # The route must re-validate the whole Delta and return 400 because
    # item_kind is invalid.
    r = app_with_draft.patch(
        f"/api/plan/draft/{draft_id}/items/medium.targets.nvda?user_id=ariel",
        json={"user_edit_note": "looks fine from user side"},
    )
    assert r.status_code == 400, r.text


def test_post_delta_accept_404_when_item_id_missing(app_with_draft):
    sess = app_with_draft.app.state.session_factory()
    try:
        from argosy.state.queries import get_pending_draft
        draft_id = get_pending_draft(sess, "ariel").id
    finally:
        sess.close()

    r = app_with_draft.post(
        f"/api/plan/draft/{draft_id}/items/nope/accept?user_id=ariel"
    )
    assert r.status_code == 404


def test_accept_publishes_plan_current_changed(app_with_draft, monkeypatch):
    """Accepting a draft must publish plan.current.changed."""
    from argosy.api.routes import plan as plan_routes

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(plan_routes, "_publish", lambda et, payload: events.append((et, payload)))

    r1 = app_with_draft.get("/api/plan/draft?user_id=ariel")
    draft_id = r1.json()["plan_version_id"]
    app_with_draft.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")

    types = [e[0] for e in events]
    assert "plan.draft.accepted" in types
    assert "plan.current.changed" in types


# ---------------------------------------------------------------------------
# I1 — home-brief cache invalidation on draft lifecycle changes
# ---------------------------------------------------------------------------


def test_accept_invalidates_home_brief_cache(app_with_draft, monkeypatch):
    """post_draft_accept must call invalidate_home_brief after commit."""
    from argosy.api.routes import plan as plan_routes

    purged: list[str] = []
    monkeypatch.setattr(
        plan_routes, "invalidate_home_brief", lambda uid: purged.append(uid)
    )

    r1 = app_with_draft.get("/api/plan/draft?user_id=ariel")
    draft_id = r1.json()["plan_version_id"]
    app_with_draft.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")

    assert "ariel" in purged, f"cache purge not called; purged={purged}"


def test_reject_invalidates_home_brief_cache(app_with_draft, monkeypatch):
    """post_draft_reject must call invalidate_home_brief after commit."""
    from argosy.api.routes import plan as plan_routes

    purged: list[str] = []
    monkeypatch.setattr(
        plan_routes, "invalidate_home_brief", lambda uid: purged.append(uid)
    )

    r1 = app_with_draft.get("/api/plan/draft?user_id=ariel")
    draft_id = r1.json()["plan_version_id"]
    app_with_draft.post(
        f"/api/plan/draft/{draft_id}/reject?user_id=ariel",
        json={"reason": "too cautious"},
    )

    assert "ariel" in purged, f"cache purge not called; purged={purged}"


def test_delta_accept_invalidates_home_brief_cache(app_with_draft, monkeypatch):
    """post_delta_accept must call invalidate_home_brief after commit."""
    from argosy.api.routes import plan as plan_routes

    # Inject a delta first.
    sess = app_with_draft.app.state.session_factory()
    try:
        from argosy.state.queries import get_pending_draft
        import json as _j
        pv = get_pending_draft(sess, "ariel")
        med = _j.loads(pv.horizon_medium_json)
        med["deltas_from_prior"] = [{
            "item_kind": "target",
            "item_id": "medium.targets.nvda",
            "horizon": "medium",
            "change_kind": "modified",
            "summary": "test",
            "prior": {"value": 0.15},
            "proposed": {"value": 0.12},
            "rationale": "test",
            "cited_sources": [],
            "accepted": False,
            "user_edited": False,
            "user_edit_note": None,
        }]
        pv.horizon_medium_json = _j.dumps(med)
        sess.commit()
        draft_id = pv.id
    finally:
        sess.close()

    purged: list[str] = []
    monkeypatch.setattr(
        plan_routes, "invalidate_home_brief", lambda uid: purged.append(uid)
    )

    app_with_draft.post(
        f"/api/plan/draft/{draft_id}/items/medium.targets.nvda/accept?user_id=ariel"
    )

    assert "ariel" in purged, f"cache purge not called; purged={purged}"


def test_get_current_structured_returns_current_plan(client_with_db):
    """T3.5: GET /api/plan/current/structured returns the role='current' plan
    in the DraftResponse shape."""
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        sess.add(PlanVersion(
            user_id="ariel",
            role="current",
            version_label="synth-2026-04-accepted",
            raw_markdown="",
            horizon_long_md="# Long",
            horizon_medium_md="# Medium",
            horizon_short_md="# Short",
            horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
            horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x"}',
            horizon_short_json=(
                '{"horizon":"short","freshness_expected":"monthly","status":"no_change",'
                '"posture":"x","speculative_candidates":['
                '{"ticker":"HOOD","thesis_summary":"momentum",'
                '"suggested_position_usd":800,"suggested_position_pct_of_net_worth":0.0008,'
                '"risk_ceiling_check":true,"horizon_days":30,"expected_drawdown_pct":0.2,'
                '"exit_trigger":"stop -20%, take +50%","sourced_from":["sentiment"]}'
                ']}'
            ),
        ))
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.get("/api/plan/current/structured?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_version_id"] is not None
    assert body["horizon_short"]["horizon"] == "short"
    cands = body["horizon_short"]["speculative_candidates"]
    assert len(cands) == 1
    assert cands[0]["ticker"] == "HOOD"


def test_get_current_structured_404_when_no_current(client_with_db):
    """T3.5: 404 when the user has no role='current' plan."""
    r = client_with_db.get("/api/plan/current/structured?user_id=newcomer")
    assert r.status_code == 404


def test_delta_edit_invalidates_home_brief_cache(app_with_draft, monkeypatch):
    """patch_delta_edit must call invalidate_home_brief after commit."""
    from argosy.api.routes import plan as plan_routes

    # Inject a delta first.
    sess = app_with_draft.app.state.session_factory()
    try:
        from argosy.state.queries import get_pending_draft
        import json as _j
        pv = get_pending_draft(sess, "ariel")
        med = _j.loads(pv.horizon_medium_json)
        med["deltas_from_prior"] = [{
            "item_kind": "target",
            "item_id": "medium.targets.nvda",
            "horizon": "medium",
            "change_kind": "modified",
            "summary": "test",
            "prior": {"value": 0.15},
            "proposed": {"value": 0.12},
            "rationale": "test",
            "cited_sources": [],
            "accepted": False,
            "user_edited": False,
            "user_edit_note": None,
        }]
        pv.horizon_medium_json = _j.dumps(med)
        sess.commit()
        draft_id = pv.id
    finally:
        sess.close()

    purged: list[str] = []
    monkeypatch.setattr(
        plan_routes, "invalidate_home_brief", lambda uid: purged.append(uid)
    )

    app_with_draft.patch(
        f"/api/plan/draft/{draft_id}/items/medium.targets.nvda?user_id=ariel",
        json={"proposed": {"value": 0.11}},
    )

    assert "ariel" in purged, f"cache purge not called; purged={purged}"
