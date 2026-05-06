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
