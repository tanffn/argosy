"""Tests for POST /api/plan/rollback — revert current plan to a superseded version.

TDD: tests written BEFORE the endpoint exists; first run must be RED (404/ImportError).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from argosy.state.models import PlanVersion, User


# ---------------------------------------------------------------------------
# Module-level gate flag: force plan_gate_enforce off so the test fixtures
# (which have no decision_run_id) don't get blocked by the promote gate.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _warn_only_gate(monkeypatch):
    monkeypatch.setenv("ARGOSY_PLAN_GATE_ENFORCE", "false")
    from argosy.config import reload_settings

    reload_settings()
    yield
    reload_settings()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_versions(session: Session) -> tuple[int, int]:
    """Seed a current + a superseded PlanVersion for user 'ariel'.

    Returns (superseded_id, current_id).
    """
    from datetime import datetime, timezone

    if session.get(User, "ariel") is None:
        session.add(User(id="ariel", plan="free"))
        session.commit()

    superseded = PlanVersion(
        user_id="ariel",
        role="superseded",
        raw_markdown="# Old Plan",
        version_label="v1-superseded",
    )
    current = PlanVersion(
        user_id="ariel",
        role="current",
        raw_markdown="# Current Plan",
        version_label="v2-current",
        accepted_at=datetime.now(timezone.utc),
    )
    session.add(superseded)
    session.add(current)
    session.commit()
    return superseded.id, current.id


# ---------------------------------------------------------------------------
# Happy-path test — the core invariant
# ---------------------------------------------------------------------------


def test_rollback_superseded_becomes_current(client_with_db):
    """Rollback to a superseded version:
    - the superseded row becomes role='current'
    - the previously-current row becomes role='superseded'
    - exactly one 'current' row exists after the call
    """
    sess: Session = client_with_db.app.state.session_factory()
    try:
        superseded_id, current_id = _seed_versions(sess)
    finally:
        sess.close()

    r = client_with_db.post(
        "/api/plan/rollback",
        json={"user_id": "ariel", "target_version_id": superseded_id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored_version_id"] == superseded_id

    # Verify DB state
    sess2: Session = client_with_db.app.state.session_factory()
    try:
        all_versions = sess2.query(PlanVersion).filter_by(user_id="ariel").all()
        current_rows = [v for v in all_versions if v.role == "current"]
        superseded_rows = [v for v in all_versions if v.role == "superseded"]

        # Exactly one 'current' row — the previously-superseded version
        assert len(current_rows) == 1, f"Expected 1 current, got {len(current_rows)}"
        assert current_rows[0].id == superseded_id

        # The previously-current row is now superseded
        prev_current = next((v for v in superseded_rows if v.id == current_id), None)
        assert prev_current is not None, "Previously-current row should now be superseded"
        assert prev_current.superseded_at is not None
    finally:
        sess2.close()


# ---------------------------------------------------------------------------
# Idempotency / safety: already current → 400
# ---------------------------------------------------------------------------


def test_rollback_to_already_current_returns_400(client_with_db):
    """Attempting to roll back to the already-current version → 400."""
    sess: Session = client_with_db.app.state.session_factory()
    try:
        _superseded_id, current_id = _seed_versions(sess)
    finally:
        sess.close()

    r = client_with_db.post(
        "/api/plan/rollback",
        json={"user_id": "ariel", "target_version_id": current_id},
    )
    assert r.status_code == 400, r.text
    assert "already current" in r.json().get("detail", "").lower()


# ---------------------------------------------------------------------------
# 404 — target version does not exist
# ---------------------------------------------------------------------------


def test_rollback_nonexistent_version_returns_404(client_with_db):
    """Target version ID that doesn't exist → 404."""
    sess: Session = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
    finally:
        sess.close()

    r = client_with_db.post(
        "/api/plan/rollback",
        json={"user_id": "ariel", "target_version_id": 999999},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 400 — wrong user (other user's version)
# ---------------------------------------------------------------------------


def test_rollback_other_user_version_returns_404(client_with_db):
    """Version belonging to a different user → 404 (not leak ownership)."""
    sess: Session = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        if sess.get(User, "noga") is None:
            sess.add(User(id="noga", plan="free"))
        sess.commit()

        other_version = PlanVersion(
            user_id="noga",
            role="superseded",
            raw_markdown="# Noga Plan",
        )
        sess.add(other_version)
        sess.commit()
        other_id = other_version.id
    finally:
        sess.close()

    r = client_with_db.post(
        "/api/plan/rollback",
        json={"user_id": "ariel", "target_version_id": other_id},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 400 — cannot roll back to a draft
# ---------------------------------------------------------------------------


def test_rollback_to_draft_returns_400(client_with_db):
    """Attempting to roll back to a draft version → 400 (only superseded allowed)."""
    sess: Session = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()

        draft = PlanVersion(
            user_id="ariel",
            role="draft",
            raw_markdown="# Draft Plan",
        )
        sess.add(draft)
        sess.commit()
        draft_id = draft.id
    finally:
        sess.close()

    r = client_with_db.post(
        "/api/plan/rollback",
        json={"user_id": "ariel", "target_version_id": draft_id},
    )
    assert r.status_code == 400, r.text
    detail = r.json().get("detail", "").lower()
    assert "superseded" in detail or "draft" in detail


# ---------------------------------------------------------------------------
# No duplicate 'current' rows invariant (explicit assertion)
# ---------------------------------------------------------------------------


def test_rollback_no_duplicate_current_rows(client_with_db):
    """After rollback, DB must have exactly one role='current' row for the user."""
    sess: Session = client_with_db.app.state.session_factory()
    try:
        superseded_id, _current_id = _seed_versions(sess)
    finally:
        sess.close()

    r = client_with_db.post(
        "/api/plan/rollback",
        json={"user_id": "ariel", "target_version_id": superseded_id},
    )
    assert r.status_code == 200, r.text

    sess2: Session = client_with_db.app.state.session_factory()
    try:
        current_count = (
            sess2.query(PlanVersion)
            .filter_by(user_id="ariel", role="current")
            .count()
        )
        assert current_count == 1, f"Expected exactly 1 current row, got {current_count}"
    finally:
        sess2.close()


# ---------------------------------------------------------------------------
# Edge case: no current plan exists — rollback promotes the target anyway
# ---------------------------------------------------------------------------


def test_rollback_when_no_current_plan_promotes_target(client_with_db):
    """When no current plan exists, rollback should still promote the superseded version."""
    sess: Session = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()

        orphan = PlanVersion(
            user_id="ariel",
            role="superseded",
            raw_markdown="# Orphaned superseded plan",
            version_label="v0",
        )
        sess.add(orphan)
        sess.commit()
        orphan_id = orphan.id
    finally:
        sess.close()

    r = client_with_db.post(
        "/api/plan/rollback",
        json={"user_id": "ariel", "target_version_id": orphan_id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored_version_id"] == orphan_id

    sess2: Session = client_with_db.app.state.session_factory()
    try:
        v = sess2.get(PlanVersion, orphan_id)
        assert v is not None
        assert v.role == "current"
        assert v.accepted_at is not None
    finally:
        sess2.close()
