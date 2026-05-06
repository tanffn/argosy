"""Queries for plan_versions lifecycle access — see spec §5."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from argosy.state.models import PlanVersion, User


@pytest.fixture
def session_with_users(alembic_engine_at_head):
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    sess = SessionLocal()
    sess.add(User(id="ariel", plan="free"))
    sess.add(User(id="dana", plan="free"))
    sess.commit()
    yield sess
    sess.close()


def _make(sess: Session, **kw) -> PlanVersion:
    pv = PlanVersion(**kw)
    sess.add(pv)
    sess.commit()
    sess.refresh(pv)
    return pv


def test_get_active_baseline_returns_only_role_baseline(session_with_users):
    from argosy.state.queries import get_active_baseline

    sess = session_with_users
    _make(sess, user_id="ariel", role="baseline", version_label="Jacobs v2.0", raw_markdown="# Plan")
    _make(sess, user_id="ariel", role="superseded", version_label="Jacobs v1.0", raw_markdown="# Old")

    pv = get_active_baseline(sess, "ariel")
    assert pv is not None
    assert pv.role == "baseline"
    assert pv.version_label == "Jacobs v2.0"


def test_get_active_baseline_returns_none_when_absent(session_with_users):
    from argosy.state.queries import get_active_baseline

    pv = get_active_baseline(session_with_users, "dana")
    assert pv is None


def test_get_current_plan_returns_role_current(session_with_users):
    from argosy.state.queries import get_current_plan

    sess = session_with_users
    _make(sess, user_id="ariel", role="baseline", version_label="Jacobs v2.0", raw_markdown="# Plan")
    _make(sess, user_id="ariel", role="current", version_label="synth-2026-05", raw_markdown="")

    pv = get_current_plan(sess, "ariel")
    assert pv is not None
    assert pv.role == "current"


def test_get_pending_draft_returns_role_draft(session_with_users):
    from argosy.state.queries import get_pending_draft

    sess = session_with_users
    _make(sess, user_id="ariel", role="baseline", version_label="Jacobs v2.0", raw_markdown="# Plan")
    _make(sess, user_id="ariel", role="draft", version_label="synth-2026-06-draft", raw_markdown="")

    pv = get_pending_draft(sess, "ariel")
    assert pv is not None
    assert pv.role == "draft"


def test_at_most_one_baseline_per_user(session_with_users):
    """The partial unique index from migration 0015 must reject duplicates."""
    from sqlalchemy.exc import IntegrityError

    sess = session_with_users
    _make(sess, user_id="ariel", role="baseline", version_label="A", raw_markdown="")
    with pytest.raises(IntegrityError):
        _make(sess, user_id="ariel", role="baseline", version_label="B", raw_markdown="")
