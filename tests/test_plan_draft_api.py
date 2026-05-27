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
        # show up as "skipped" — they no longer inflate agents_failed
        # (skipped is tracked in its own bucket now) but this test pins
        # the stronger invariant that every expected agent ran.
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
        # codex_second_opinion row — the FM-rooted tree builder hangs a
        # codex node under FM (see agent_tree_builder._build_codex_node).
        # Without a row, the node renders as ``skipped`` (no longer
        # ``failed`` since the split). The test still seeds it so the
        # stronger "every agent ran" assertion holds: agents_failed == 0
        # AND agents_skipped == 0.
        sess.add(AgentReport(
            user_id="ariel", agent_role="codex_second_opinion",
            decision_id=decision_id_str,
            response_text=json.dumps({
                "overall_assessment": "APPROVE",
                "findings": [],
                "agreement_with_argosy": {
                    "agrees_with_risk_verdict": True,
                    "novel_concerns_argosy_missed": [],
                },
            }),
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
    # Every expected agent ran -> agents_failed = 0 AND agents_skipped = 0;
    # agents_ok must be >= 1. The split exists so future topology additions
    # (e.g. codex_second_opinion when it shipped) don't silently inflate
    # the user-facing "failed" count just because seed data is missing.
    assert health["agents_failed"] == 0
    assert health["agents_skipped"] == 0
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


def test_get_draft_objections_includes_prior_round_when_superseded_predecessor(
    app_with_draft,
):
    """When the draft has a ``role='superseded'`` predecessor that itself
    carries a fund_manager agent_report, the objections endpoint returns
    those objections under ``prior_round_objections`` so the UI can map
    "Blocker #N" tokens in delta rationales to the prior-round objection
    by index.
    """
    from datetime import datetime, timedelta, timezone

    from argosy.state.models import AgentReport, PlanVersion

    sess = app_with_draft.app.state.session_factory()
    try:
        # Find the current draft (the fixture's pending draft).
        draft = sess.query(PlanVersion).filter_by(
            user_id="ariel", role="draft"
        ).one()
        # Wire it to a synth run + give it its own FM verdict so the
        # endpoint executes the full happy path.
        draft.decision_run_id = 200
        sess.add(AgentReport(
            user_id="ariel",
            agent_role="fund_manager",
            decision_id="plan-synth-200",
            response_text=json.dumps({
                "approved": False,
                "reasons": [
                    "NEW BLOCKER — fresh problem in this draft",
                ],
                "cited_sources": [],
            }),
            model="claude-opus-4-7",
        ))

        # Insert a superseded predecessor for this user with an EARLIER
        # imported_at and its own FM verdict carrying 3 objections.
        # The route picks "most recent superseded with imported_at < draft.imported_at".
        earlier = (draft.imported_at or datetime.now(timezone.utc)) - timedelta(hours=1)
        prior_draft = PlanVersion(
            user_id="ariel",
            role="superseded",
            version_label="synth-2026-04-prior",
            raw_markdown="",
            decision_run_id=199,
            imported_at=earlier,
        )
        sess.add(prior_draft)
        sess.flush()  # populate prior_draft.id

        sess.add(AgentReport(
            user_id="ariel",
            agent_role="fund_manager",
            decision_id="plan-synth-199",
            response_text=json.dumps({
                "approved": False,
                "reasons": [
                    "[BLOCKER — tax-rate citation] NVDA tranche tax estimate uses 30.7%; the audit confirms 32.5%.",
                    "[BLOCKER — UCITS coherence] SGOV → XEON migration sequenced behind the wrong gate.",
                    "[BLOCKER — cross-horizon coherence] 45% medium-horizon NVDA gate price-anchor is inconsistent.",
                ],
                "cited_sources": [],
            }),
            model="claude-opus-4-7",
        ))
        sess.commit()
    finally:
        sess.close()

    r = app_with_draft.get("/api/plan/draft/objections?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()

    # Current draft still has its own one objection.
    assert len(body["objections"]) == 1
    assert "fresh problem" in body["objections"][0]["detail"]

    # Prior round objections — 3 entries, ordered by reasons[] index so
    # "Blocker #3" in the new delta rationale maps to entries[2].
    assert "prior_round_objections" in body
    prior = body["prior_round_objections"]
    assert len(prior) == 3
    assert "tax-rate citation" in prior[0]["topic"].lower() \
        or "tax-rate citation" in prior[0]["detail"].lower()
    assert "UCITS" in prior[1]["topic"] or "UCITS" in prior[1]["detail"]
    assert "cross-horizon" in prior[2]["topic"].lower() \
        or "cross-horizon" in prior[2]["detail"].lower()
    # Severity inferred from the BLOCKER keyword.
    for obj in prior:
        assert obj["severity"] in ("RED", "AMBER", "YELLOW")


def test_get_draft_objections_prior_round_empty_when_no_predecessor(
    app_with_draft,
):
    """No superseded predecessor -> ``prior_round_objections`` is an empty
    list, never null.  The current draft's own objections are unaffected.
    """
    from argosy.state.models import AgentReport, PlanVersion

    sess = app_with_draft.app.state.session_factory()
    try:
        draft = sess.query(PlanVersion).filter_by(
            user_id="ariel", role="draft"
        ).one()
        draft.decision_run_id = 300
        sess.add(AgentReport(
            user_id="ariel",
            agent_role="fund_manager",
            decision_id="plan-synth-300",
            response_text=json.dumps({
                "approved": False,
                "reasons": ["MISSING — something"],
                "cited_sources": [],
            }),
            model="claude-opus-4-7",
        ))
        sess.commit()
    finally:
        sess.close()

    r = app_with_draft.get("/api/plan/draft/objections?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["prior_round_objections"] == []


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


# ---------------------------------------------------------------------------
# NVDA pace — lifted from the latest concentration agent_report (home page).
# ---------------------------------------------------------------------------


def test_get_draft_nvda_pace_from_concentration_report(app_with_draft):
    """When the draft has a concentration agent_report with a non-default
    nvda_pace block, ``GET /api/plan/draft`` surfaces those values verbatim
    so the home page's NVDA PACE tile shows real numbers (not 0/10,000).
    """
    from argosy.state.models import AgentReport, PlanVersion

    sess = app_with_draft.app.state.session_factory()
    try:
        draft = sess.query(PlanVersion).filter_by(
            user_id="ariel", role="draft"
        ).one()
        draft.decision_run_id = 42
        # The real concentration agent emits ```` ```json ... ``` ````-fenced
        # JSON; the route's lenient decoder must strip the fence. Use the
        # same shape here so we exercise that code path.
        sess.add(AgentReport(
            user_id="ariel",
            agent_role="concentration",
            decision_id="plan-synth-42",
            response_text=(
                "```json\n"
                + json.dumps({
                    "breaches": [],
                    "deltas_vs_target": {},
                    "nvda_pace": {
                        "shares_sold_ytd": 2000,
                        "target_shares_ytd": 4000,
                        "delta_shares": -2000,
                        "on_track": False,
                    },
                    "summary": "behind plan",
                    "confidence": "HIGH",
                    "cited_sources": ["portfolio/holdings"],
                })
                + "\n```"
            ),
            model="claude-sonnet-4-6",
        ))
        sess.commit()
    finally:
        sess.close()

    r = app_with_draft.get("/api/plan/draft?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "nvda_pace" in body
    pace = body["nvda_pace"]
    assert pace is not None
    assert pace["shares_sold_ytd"] == 2000
    assert pace["target_shares_ytd"] == 4000
    assert pace["delta_shares"] == -2000
    assert pace["on_track"] is False


def test_get_draft_nvda_pace_null_when_no_concentration_report(app_with_draft):
    """A draft with no backing decision_run_id (the default fixture state)
    or no concentration agent_report degrades cleanly to ``nvda_pace=None``.
    The home page renders an "Awaiting synthesis run" tooltip in that case.
    """
    r = app_with_draft.get("/api/plan/draft?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "nvda_pace" in body
    assert body["nvda_pace"] is None


def test_get_draft_nvda_pace_null_when_response_text_malformed(app_with_draft):
    """A concentration agent_report whose response_text isn't parseable JSON
    must not crash the route — the field simply degrades to ``None``.
    """
    from argosy.state.models import AgentReport, PlanVersion

    sess = app_with_draft.app.state.session_factory()
    try:
        draft = sess.query(PlanVersion).filter_by(
            user_id="ariel", role="draft"
        ).one()
        draft.decision_run_id = 7
        sess.add(AgentReport(
            user_id="ariel",
            agent_role="concentration",
            decision_id="plan-synth-7",
            response_text="<not-json> totally unparseable",
            model="claude-sonnet-4-6",
        ))
        sess.commit()
    finally:
        sess.close()

    r = app_with_draft.get("/api/plan/draft?user_id=ariel")
    assert r.status_code == 200, r.text
    assert r.json()["nvda_pace"] is None


# ---------------------------------------------------------------------------
# /api/plan/in-flight-synthesis — surfaces an in-flight plan_revision
# decision_run so the /plan page can render a "Synthesis #N · phase X of 5"
# card when the prior draft was superseded by the kickoff and /api/plan/draft
# 404s. See argosy.api.routes.plan.get_in_flight_synthesis for the route.
# ---------------------------------------------------------------------------


def test_in_flight_synthesis_returns_running_run_with_phase_count(client_with_db):
    """In-flight plan_revision run + no draft -> endpoint returns the
    in-flight payload with ``completed_phases`` matching the count of
    ``decision_phases`` rows that have ``finished_at IS NOT NULL`` for
    the matched run.
    """
    from datetime import datetime, timezone

    from argosy.state.models import DecisionPhase, DecisionRun, User

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        now = datetime.now(timezone.utc)
        run = DecisionRun(
            user_id="ariel",
            ticker="(plan)",
            tier=None,
            decision_kind="plan_revision",
            status="running",
            started_at=now,
        )
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        rid = run.id

        # Two phases finished, one started but not finished — the
        # endpoint must count only the finished ones.
        sess.add(DecisionPhase(
            decision_run_id=rid, user_id="ariel", seq=1,
            kind="synthesis.phase_1",
            started_at=now, finished_at=now,
            participants_json="[]",
        ))
        sess.add(DecisionPhase(
            decision_run_id=rid, user_id="ariel", seq=2,
            kind="synthesis.phase_2",
            started_at=now, finished_at=now,
            participants_json="[]",
        ))
        sess.add(DecisionPhase(
            decision_run_id=rid, user_id="ariel", seq=3,
            kind="synthesis.phase_3",
            started_at=now, finished_at=None,
            participants_json="[]",
        ))
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.get("/api/plan/in-flight-synthesis?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    payload = body["in_flight_synthesis"]
    assert payload is not None
    assert payload["decision_run_id"] == rid
    assert payload["decision_audit_token"] == f"plan-synth-{rid}"
    assert payload["completed_phases"] == 2
    assert payload["total_phases"] == 5
    assert payload["status"] == "running"
    assert payload["started_at"]  # ISO timestamp present


def test_in_flight_synthesis_returns_null_when_nothing_running(client_with_db):
    """No in-flight run + no draft -> endpoint returns 200 with null.

    The polling loop on /plan calls this every 10 s; returning 404 would
    pollute the network panel and force the UI into a try/except dance.
    """
    from argosy.state.models import User

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
    finally:
        sess.close()

    r = client_with_db.get("/api/plan/in-flight-synthesis?user_id=ariel")
    assert r.status_code == 200, r.text
    assert r.json() == {"in_flight_synthesis": None}


def test_in_flight_synthesis_ignores_completed_runs(client_with_db):
    """A completed plan_revision run must NOT be reported as in-flight.

    The endpoint filters ``status='running'`` precisely so a finished
    synthesis (whose draft was then accepted/superseded) doesn't keep
    the /plan card stuck on "synthesizing".
    """
    from datetime import datetime, timezone

    from argosy.state.models import DecisionRun, User

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        now = datetime.now(timezone.utc)
        sess.add(DecisionRun(
            user_id="ariel",
            ticker="(plan)",
            tier=None,
            decision_kind="plan_revision",
            status="completed",
            started_at=now,
            finished_at=now,
        ))
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.get("/api/plan/in-flight-synthesis?user_id=ariel")
    assert r.status_code == 200, r.text
    assert r.json() == {"in_flight_synthesis": None}


def test_in_flight_synthesis_picks_latest_running_run(client_with_db):
    """When multiple running plan_revision runs exist (a regression that
    shouldn't happen but might during fleet bugs) the endpoint picks the
    one with the highest ``id`` so the UI tracks the freshest kickoff.
    """
    from datetime import datetime, timezone

    from argosy.state.models import DecisionRun, User

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        now = datetime.now(timezone.utc)
        sess.add(DecisionRun(
            user_id="ariel", ticker="(plan)", tier=None,
            decision_kind="plan_revision", status="running",
            started_at=now,
        ))
        sess.add(DecisionRun(
            user_id="ariel", ticker="(plan)", tier=None,
            decision_kind="plan_revision", status="running",
            started_at=now,
        ))
        sess.commit()
        latest_id = sess.execute(
            __import__("sqlalchemy").select(DecisionRun.id)
            .where(
                DecisionRun.user_id == "ariel",
                DecisionRun.status == "running",
            )
            .order_by(__import__("sqlalchemy").desc(DecisionRun.id))
        ).scalars().first()
    finally:
        sess.close()

    r = client_with_db.get("/api/plan/in-flight-synthesis?user_id=ariel")
    assert r.status_code == 200, r.text
    payload = r.json()["in_flight_synthesis"]
    assert payload is not None
    assert payload["decision_run_id"] == latest_id


def test_in_flight_synthesis_scoped_to_user(client_with_db):
    """A running plan_revision run for another user must not leak into
    this user's in-flight response. Multi-tenancy hygiene.
    """
    from datetime import datetime, timezone

    from argosy.state.models import DecisionRun, User

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        if sess.get(User, "other") is None:
            sess.add(User(id="other", plan="free"))
        sess.commit()
        sess.add(DecisionRun(
            user_id="other", ticker="(plan)", tier=None,
            decision_kind="plan_revision", status="running",
            started_at=datetime.now(timezone.utc),
        ))
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.get("/api/plan/in-flight-synthesis?user_id=ariel")
    assert r.status_code == 200, r.text
    assert r.json() == {"in_flight_synthesis": None}


def test_in_flight_synthesis_works_alongside_pending_draft(app_with_draft):
    """Transition state: a pending draft exists AND a fresh synthesis is
    running for a re-revision. The endpoint reports the in-flight run; the
    /plan page reads both ``/api/plan/draft`` and
    ``/api/plan/in-flight-synthesis`` and decides which UI to render.
    """
    from datetime import datetime, timezone

    from argosy.state.models import DecisionRun

    sess = app_with_draft.app.state.session_factory()
    try:
        sess.add(DecisionRun(
            user_id="ariel",
            ticker="(plan)",
            tier=None,
            decision_kind="plan_revision",
            status="running",
            started_at=datetime.now(timezone.utc),
        ))
        sess.commit()
    finally:
        sess.close()

    # Draft endpoint still returns the pending draft.
    r1 = app_with_draft.get("/api/plan/draft?user_id=ariel")
    assert r1.status_code == 200
    assert r1.json()["plan_version_id"] is not None

    # In-flight endpoint reports the running run alongside.
    r2 = app_with_draft.get("/api/plan/in-flight-synthesis?user_id=ariel")
    assert r2.status_code == 200, r2.text
    payload = r2.json()["in_flight_synthesis"]
    assert payload is not None
    assert payload["status"] == "running"


def test_in_flight_synthesis_does_not_pick_other_decision_kinds(client_with_db):
    """A running ``trade_proposal`` or ``plan_amendment_chat`` run must NOT
    be reported as a plan_revision in-flight synthesis. Only the plan
    synthesis surface uses this card.
    """
    from datetime import datetime, timezone

    from argosy.state.models import DecisionRun, User

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        now = datetime.now(timezone.utc)
        sess.add(DecisionRun(
            user_id="ariel", ticker="NVDA", tier="T0",
            decision_kind="trade_proposal", status="running",
            started_at=now,
        ))
        sess.add(DecisionRun(
            user_id="ariel", ticker="(plan)", tier="small",
            decision_kind="plan_amendment_chat", status="running",
            started_at=now,
        ))
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.get("/api/plan/in-flight-synthesis?user_id=ariel")
    assert r.status_code == 200, r.text
    assert r.json() == {"in_flight_synthesis": None}


def test_cashflow_projection_route_returns_series(client_with_db):
    """Smoke: full route returns a 30-year monthly series + retire-ready age."""
    from tests.test_cashflow_projection import _seed_full_state
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        _seed_full_state(s)
    r = client_with_db.get("/api/plan/draft/cashflow-projection?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert "series" in body
    assert len(body["series"]) == 30 * 12 + 1
    assert body["fx_usd_nis"] == pytest.approx(2.94)
    first = body["series"][0]
    assert first["months_out"] == 0
    # 23,084 NIS / 2.94 ≈ 7,851 USD
    assert first["expenses_monthly_usd"] == pytest.approx(7_851.0, rel=1e-2)


def test_cashflow_projection_retirement_age_param(client_with_db):
    """Different ``retirement_age`` → different pension annuity at 67."""
    from tests.test_cashflow_projection import _seed_full_state
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        _seed_full_state(s)
    r1 = client_with_db.get(
        "/api/plan/draft/cashflow-projection?user_id=ariel&retirement_age=49"
    )
    r2 = client_with_db.get(
        "/api/plan/draft/cashflow-projection?user_id=ariel&retirement_age=60"
    )
    assert r1.status_code == 200 and r2.status_code == 200

    def first_at_67(body):
        for p in body["series"]:
            if p["age_years"] >= 67.0:
                return p
        return None

    p49 = first_at_67(r1.json())
    p60 = first_at_67(r2.json())
    assert p49 is not None and p60 is not None
    assert p60["pension_annuity_monthly_usd"] > p49["pension_annuity_monthly_usd"]


def test_cashflow_projection_tax_rate_param(client_with_db):
    """tax_rate=0 should yield higher portfolio income than tax_rate=0.5."""
    from tests.test_cashflow_projection import _seed_full_state
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        _seed_full_state(s)
    r0 = client_with_db.get(
        "/api/plan/draft/cashflow-projection?user_id=ariel&tax_rate=0.0"
    )
    r50 = client_with_db.get(
        "/api/plan/draft/cashflow-projection?user_id=ariel&tax_rate=0.5"
    )
    assert r0.status_code == 200 and r50.status_code == 200
    inc_0 = r0.json()["series"][0]["portfolio_income_base_monthly_usd"]
    inc_50 = r50.json()["series"][0]["portfolio_income_base_monthly_usd"]
    assert inc_50 == pytest.approx(inc_0 * 0.5, rel=1e-3)


def test_cashflow_projection_portfolio_override(client_with_db):
    """portfolio_value_usd_override should replace the DB-computed value."""
    from tests.test_cashflow_projection import _seed_full_state
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        _seed_full_state(s)
    # Seeded portfolio is $1.5M USD (total_usd_value_k=1500). Override to $1M.
    r = client_with_db.get(
        "/api/plan/draft/cashflow-projection?user_id=ariel&portfolio_value_usd_override=1000000"
    )
    assert r.status_code == 200
    body = r.json()
    # t=0 portfolio income should be ~$1M * 0.055 * 0.75 / 12 = $3,437.50/mo
    # (mu_nominal=0.08, inflation=0.025, real=0.055, net after 25% tax)
    p0 = body["series"][0]
    expected = 1_000_000 * (0.08 - 0.025) * (1 - 0.25) / 12
    assert p0["portfolio_income_base_monthly_usd"] == pytest.approx(
        expected, rel=1e-2
    )


def test_cashflow_projection_mu_nominal_param(client_with_db):
    """mu_nominal=0.04 should give half the real return of mu=0.055.

    real_return = mu - inflation = 0.04 - 0.025 = 0.015 (vs default 0.055).
    Portfolio income drops accordingly."""
    from tests.test_cashflow_projection import _seed_full_state
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        _seed_full_state(s)
    r_default = client_with_db.get(
        "/api/plan/draft/cashflow-projection?user_id=ariel"
    )
    r_low = client_with_db.get(
        "/api/plan/draft/cashflow-projection?user_id=ariel&mu_nominal_annual=0.04"
    )
    assert r_default.status_code == 200 and r_low.status_code == 200
    inc_default = r_default.json()["series"][0]["portfolio_income_base_monthly_usd"]
    inc_low = r_low.json()["series"][0]["portfolio_income_base_monthly_usd"]
    # default real 0.055, low real 0.015 → ratio 0.015/0.055 ≈ 0.273
    assert inc_low == pytest.approx(inc_default * (0.015 / 0.055), rel=1e-2)


def test_cashflow_projection_mu_bounds(client_with_db):
    """mu_nominal outside [0.02, 0.15] is rejected."""
    from tests.test_cashflow_projection import _seed_full_state
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        _seed_full_state(s)
    # Below range
    r = client_with_db.get(
        "/api/plan/draft/cashflow-projection?user_id=ariel&mu_nominal_annual=0.01"
    )
    assert r.status_code == 422
    # Above range
    r = client_with_db.get(
        "/api/plan/draft/cashflow-projection?user_id=ariel&mu_nominal_annual=0.20"
    )
    assert r.status_code == 422
