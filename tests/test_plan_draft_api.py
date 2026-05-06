"""Draft lifecycle endpoints — see spec §7.6."""

from __future__ import annotations

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
