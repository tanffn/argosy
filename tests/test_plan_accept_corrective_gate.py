"""Accept-route corrective gate + promote hook.

Design: docs/design/corrective_resynthesis.md §2.C.3 / §5 (accept path:
unresolved blocks without override; promote flips fed proposals).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from argosy.state.models import ActionProposal, PlanCritique, PlanVersion, User


@pytest.fixture(autouse=True)
def _no_live_narrative_regen(monkeypatch):
    """/accept fires a fire-and-forget PlanNarrativeAgent warm (a REAL Opus
    call — no pytest kill switch in that path). Stub it: these tests exercise
    the corrective gate + promote hook, not the narrative cache."""
    from argosy.api.routes import plan as plan_routes

    monkeypatch.setattr(
        plan_routes, "_auto_regen_narrative", lambda *a, **k: None,
    )


@pytest.fixture(autouse=True)
def _warn_only_gate(monkeypatch):
    """Accept MECHANICS tests — pin the deterministic gate to warn-only so the
    minimal fixture drafts (no decision_run_id → numeric gate fail-closes)
    still promote. Same pattern as tests/test_plan_draft_api.py."""
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "false")
    from argosy.config import reload_settings

    reload_settings()
    yield
    reload_settings()


def _seed_draft(client, *, corrective=None, corrective_unresolved=None):
    sess = client.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
        inputs: dict = {"baseline_id": 1}
        if corrective is not None:
            inputs["corrective"] = corrective
        if corrective_unresolved is not None:
            inputs["corrective_unresolved"] = corrective_unresolved
        draft = PlanVersion(
            user_id="ariel", role="draft", version_label="synth-corrective",
            raw_markdown="", horizon_long_md="# Long",
            horizon_medium_md="# Medium", horizon_short_md="# Short",
            synthesis_inputs_json=json.dumps(inputs),
        )
        sess.add(draft)
        sess.commit()
        return draft.id
    finally:
        sess.close()


def _seed_proposal(client, *, status="open", dedup_key="critique_resynth:ariel",
                   kind="replan_full"):
    sess = client.app.state.session_factory()
    try:
        now = datetime.now(timezone.utc)
        row = ActionProposal(
            user_id="ariel", summary="re-synthesis needed",
            rationale_md="findings", suggested_payload="{}",
            severity="warning", surfaced_at=now,
            expires_at=now + timedelta(days=30), status=status,
            kind=kind, dedup_key=dedup_key, execution_state="proposed",
        )
        sess.add(row)
        sess.commit()
        return row.id
    finally:
        sess.close()


def _seed_critique(client, plan_version_id):
    sess = client.app.state.session_factory()
    try:
        row = PlanCritique(
            user_id="ariel", plan_version_id=plan_version_id,
            critique_json=json.dumps({
                "findings": [],
                "reconcile": {"escalated": 2},
            }),
            model="test",
        )
        sess.add(row)
        sess.commit()
        return row.id
    finally:
        sess.close()


def test_accept_422_when_corrective_unresolved(client_with_db):
    draft_id = _seed_draft(
        client_with_db,
        corrective={"proposal_ids": [], "source_critique_id": None},
        corrective_unresolved=[
            {"index": 1, "topic": "nvda-target", "reason": "canonical absent"},
        ],
    )
    r = client_with_db.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "corrective_unresolved"
    assert detail["unresolved"][0]["topic"] == "nvda-target"
    # Draft is untouched (never discarded).
    sess = client_with_db.app.state.session_factory()
    try:
        assert sess.get(PlanVersion, draft_id).role == "draft"
    finally:
        sess.close()


def test_accept_override_corrective_promotes(client_with_db):
    draft_id = _seed_draft(
        client_with_db,
        corrective={"proposal_ids": [], "source_critique_id": None},
        corrective_unresolved=[{"index": 1, "topic": "x", "reason": "absent"}],
    )
    r = client_with_db.post(
        f"/api/plan/draft/{draft_id}/accept?user_id=ariel&override_corrective=true"
    )
    assert r.status_code == 200
    sess = client_with_db.app.state.session_factory()
    try:
        assert sess.get(PlanVersion, draft_id).role == "current"
    finally:
        sess.close()


def test_accept_clean_corrective_flips_proposals_and_annotates_critique(
    client_with_db,
):
    resynth_id = _seed_proposal(client_with_db, status="open")
    verdict_id = _seed_proposal(
        client_with_db, status="accepted", dedup_key="glide_verdict:ariel",
        kind="update_plan_assumption",
    )
    # The critique row hangs off the baseline row (id irrelevant to the hook).
    draft_id = _seed_draft(client_with_db)  # placeholder to learn ids
    sess = client_with_db.app.state.session_factory()
    try:
        baseline_id = sess.query(PlanVersion).filter_by(
            user_id="ariel", role="baseline"
        ).first().id
    finally:
        sess.close()
    critique_id = _seed_critique(client_with_db, baseline_id)
    # Re-write the draft's corrective payload with the real ids.
    sess = client_with_db.app.state.session_factory()
    try:
        draft = sess.get(PlanVersion, draft_id)
        draft.synthesis_inputs_json = json.dumps({
            "corrective": {
                "proposal_ids": [resynth_id, verdict_id],
                "source_critique_id": critique_id,
            },
            "corrective_unresolved": [],
        })
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    assert r.status_code == 200

    sess = client_with_db.app.state.session_factory()
    try:
        assert sess.get(PlanVersion, draft_id).role == "current"
        p1 = sess.get(ActionProposal, resynth_id)
        p2 = sess.get(ActionProposal, verdict_id)
        assert p1.status == "executed"
        assert p2.status == "executed"
        assert f"draft #{draft_id}" in (p1.decided_by_user_note or "")
        crit = sess.get(PlanCritique, critique_id)
        payload = json.loads(crit.critique_json)
        assert payload["reconcile"]["cleared_by_draft_id"] == draft_id
        # Pre-existing reconcile keys survive the annotation.
        assert payload["reconcile"]["escalated"] == 2
    finally:
        sess.close()


def test_accept_without_corrective_keys_is_unchanged(client_with_db):
    """A non-corrective draft (no corrective keys) promotes exactly as before."""
    draft_id = _seed_draft(client_with_db)
    r = client_with_db.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    assert r.status_code == 200
    sess = client_with_db.app.state.session_factory()
    try:
        assert sess.get(PlanVersion, draft_id).role == "current"
    finally:
        sess.close()


def test_proposal_flip_skips_foreign_and_closed_rows(client_with_db):
    """The hook only flips this user's open/accepted rows — a rejected row
    (already decided) is left alone."""
    rejected_id = _seed_proposal(
        client_with_db, status="rejected", dedup_key="old:1",
    )
    draft_id = _seed_draft(
        client_with_db,
        corrective={"proposal_ids": [rejected_id, 999999],
                    "source_critique_id": None},
        corrective_unresolved=[],
    )
    r = client_with_db.post(f"/api/plan/draft/{draft_id}/accept?user_id=ariel")
    assert r.status_code == 200
    sess = client_with_db.app.state.session_factory()
    try:
        assert sess.get(ActionProposal, rejected_id).status == "rejected"
    finally:
        sess.close()
