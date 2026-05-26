"""Tests for ``argosy.services.fm_objection_translation_cache``.

Coverage:
    (a) first call computes + persists translations to
        ``fm_objection_translations`` and returns them inline,
    (b) second call returns from cache without invoking the agent,
    (c) hash mismatch (objection text changed under the same
        objection_index) triggers re-compute + row replacement,
    (d) translator failure leaves the slot uncached so the UI can
        fall back to the on-demand button, and the endpoint must
        still return 200.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.agents.objection_translator import ObjectionTranslation
from argosy.services.fm_objection_translation_cache import (
    get_or_compute_translations,
)
from argosy.state.models import (
    AgentReport,
    Base,
    FMObjectionTranslation,
    PlanVersion,
    User,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path):
    """A file-backed SQLite session with the full schema created."""
    db_path = tmp_path / "fm_cache.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="ariel", plan="free"))
        s.flush()
        # Create a baseline + a draft so the FK targets exist. The cache
        # helper writes against the draft's plan_version_id.
        s.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
        draft = PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="t",
            raw_markdown="",
        )
        s.add(draft)
        s.commit()
        s.refresh(draft)
        s._draft_id = draft.id  # type: ignore[attr-defined]
        yield s
    finally:
        s.close()
        engine.dispose()


def _make_fake_report(headline: str, plain_english: str, actions: list[str]):
    """Build an AgentReport-shaped object the helper can introspect.

    We only need ``.output`` to be an ObjectionTranslation; the helper
    pulls headline/plain_english/recommended_actions off it. Returning
    a SimpleNamespace would be enough, but using ObjectionTranslation
    inside a tiny wrapper keeps the test honest about field names.
    """

    class _Report:
        output = ObjectionTranslation(
            headline=headline,
            plain_english=plain_english,
            recommended_actions=actions,
        )

    return _Report()


# ---------------------------------------------------------------------------
# (a) first call computes + persists
# ---------------------------------------------------------------------------


def test_first_call_computes_and_persists(session):
    """First call: each objection produces one translator call AND a
    row in ``fm_objection_translations``; returned DTOs match.
    """
    draft_id = session._draft_id
    objections = [
        {"severity": "RED", "topic": "TIME-CRITICAL", "detail": "missed section 102"},
        {"severity": "AMBER", "topic": "MISSING STOP", "detail": "no drawdown trigger"},
        {"severity": "YELLOW", "topic": "MINOR", "detail": "small thing"},
    ]

    calls: list[str] = []

    async def _fake_run(self, *, topic, detail, severity, cited_sources=None):
        calls.append(topic)
        return _make_fake_report(
            headline=f"H::{topic}",
            plain_english=f"PE::{topic}",
            actions=[f"A::{topic}"],
        )

    with patch(
        "argosy.agents.objection_translator.ObjectionTranslatorAgent.run",
        new=_fake_run,
    ):
        out = get_or_compute_translations(
            session,
            user_id="ariel",
            plan_version_id=draft_id,
            objections=objections,
        )

    # All three slots translated.
    assert set(out.keys()) == {0, 1, 2}
    assert out[0].headline == "H::TIME-CRITICAL"
    assert out[1].plain_english == "PE::MISSING STOP"
    assert out[2].recommended_actions == ["A::MINOR"]

    # Every objection got exactly one translator call (parallel batch).
    assert sorted(calls) == sorted(o["topic"] for o in objections)
    assert len(calls) == 3

    # Rows persisted (3 of them).
    rows = (
        session.execute(
            sa.select(FMObjectionTranslation).where(
                FMObjectionTranslation.plan_version_id == draft_id
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 3
    by_idx = {r.objection_index: r for r in rows}
    assert by_idx[0].headline == "H::TIME-CRITICAL"
    assert json.loads(by_idx[2].recommended_actions_json) == ["A::MINOR"]
    # topic_hash is populated and non-empty (sha256 hex digest is 64 chars).
    assert len(by_idx[0].topic_hash) == 64


# ---------------------------------------------------------------------------
# (b) second call returns from cache (no agent call)
# ---------------------------------------------------------------------------


def test_second_call_uses_cache_no_agent_call(session):
    """After the first call populates the cache, a second identical
    call must NOT invoke the translator agent at all.
    """
    draft_id = session._draft_id
    objections = [
        {"severity": "RED", "topic": "T1", "detail": "d1"},
        {"severity": "AMBER", "topic": "T2", "detail": "d2"},
    ]

    calls: list[str] = []

    async def _fake_run(self, *, topic, detail, severity, cited_sources=None):
        calls.append(topic)
        return _make_fake_report(f"H::{topic}", f"PE::{topic}", [])

    with patch(
        "argosy.agents.objection_translator.ObjectionTranslatorAgent.run",
        new=_fake_run,
    ):
        # First call: 2 translator invocations.
        out1 = get_or_compute_translations(
            session,
            user_id="ariel",
            plan_version_id=draft_id,
            objections=objections,
        )
        assert len(calls) == 2
        first_call_count = len(calls)

        # Second call with identical objections: 0 additional invocations.
        out2 = get_or_compute_translations(
            session,
            user_id="ariel",
            plan_version_id=draft_id,
            objections=objections,
        )

    assert len(calls) == first_call_count, (
        f"expected no new translator calls on cache-hit second call, "
        f"got {len(calls) - first_call_count} extra"
    )
    # Returned DTOs match cached values.
    assert out2[0].headline == out1[0].headline == "H::T1"
    assert out2[1].plain_english == out1[1].plain_english == "PE::T2"


# ---------------------------------------------------------------------------
# (c) hash mismatch triggers re-compute
# ---------------------------------------------------------------------------


def test_hash_mismatch_triggers_recompute(session):
    """If the cached row's topic_hash no longer matches the live
    objection text (FM re-evaluated under the same plan_version_id),
    the helper must re-translate that slot and replace the row.
    """
    draft_id = session._draft_id
    objections_v1 = [
        {"severity": "RED", "topic": "T1", "detail": "original detail"},
    ]
    objections_v2 = [
        # Same severity + topic, NEW detail → different hash.
        {"severity": "RED", "topic": "T1", "detail": "rewritten detail"},
    ]

    calls: list[str] = []

    async def _fake_run(self, *, topic, detail, severity, cited_sources=None):
        calls.append(f"{topic}::{detail}")
        return _make_fake_report(f"H::{topic}", f"PE::{detail}", [])

    with patch(
        "argosy.agents.objection_translator.ObjectionTranslatorAgent.run",
        new=_fake_run,
    ):
        get_or_compute_translations(
            session,
            user_id="ariel",
            plan_version_id=draft_id,
            objections=objections_v1,
        )
        assert calls == ["T1::original detail"]

        # Same draft_id, same objection_index 0, but the detail changed.
        out2 = get_or_compute_translations(
            session,
            user_id="ariel",
            plan_version_id=draft_id,
            objections=objections_v2,
        )

    # The helper should have fired a second translator call against
    # the new detail text — not returned the stale cached row.
    assert calls == ["T1::original detail", "T1::rewritten detail"]
    assert out2[0].plain_english == "PE::rewritten detail"

    # Exactly one row persisted for index 0 (old row replaced, not
    # duplicated — the UNIQUE constraint would block a dupe anyway).
    rows = (
        session.execute(
            sa.select(FMObjectionTranslation).where(
                FMObjectionTranslation.plan_version_id == draft_id,
                FMObjectionTranslation.objection_index == 0,
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].plain_english == "PE::rewritten detail"


# ---------------------------------------------------------------------------
# (d) translator failure doesn't break the endpoint
# ---------------------------------------------------------------------------


def test_translator_failure_leaves_slot_uncached_no_crash(session):
    """When the translator raises for one of N objections, the helper
    must (1) return successfully (no crash), (2) omit the failing
    slot from the result map, and (3) NOT persist a partial row for
    the failing slot.
    """
    draft_id = session._draft_id
    objections = [
        {"severity": "RED", "topic": "GOOD", "detail": "this one works"},
        {"severity": "AMBER", "topic": "BAD", "detail": "this one fails"},
    ]

    from argosy.agents.errors import AgentRunError

    calls: list[str] = []

    async def _fake_run(self, *, topic, detail, severity, cited_sources=None):
        calls.append(topic)
        if topic == "BAD":
            raise AgentRunError("simulated translator failure")
        return _make_fake_report(f"H::{topic}", f"PE::{topic}", [])

    with patch(
        "argosy.agents.objection_translator.ObjectionTranslatorAgent.run",
        new=_fake_run,
    ):
        out = get_or_compute_translations(
            session,
            user_id="ariel",
            plan_version_id=draft_id,
            objections=objections,
        )

    # Both translator slots were attempted.
    assert sorted(calls) == ["BAD", "GOOD"]
    # Only the successful slot appears in the result map.
    assert set(out.keys()) == {0}
    assert out[0].headline == "H::GOOD"

    # Only one row persisted (the failing slot was skipped).
    rows = (
        session.execute(
            sa.select(FMObjectionTranslation).where(
                FMObjectionTranslation.plan_version_id == draft_id
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].objection_index == 0


# ---------------------------------------------------------------------------
# Endpoint integration — translator failure on objection 1 must not
# break /api/plan/draft/objections; response still returns the
# objection list, just with translation=None for the failing slot.
# ---------------------------------------------------------------------------


def test_endpoint_returns_200_when_translator_fails(client_with_db):
    """End-to-end: when the cache helper raises for a slot, the route
    must still 200 and return the objection with translation=None.
    """
    from argosy.agents.errors import AgentRunError

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.flush()
        sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
        draft = PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="t",
            raw_markdown="",
            decision_run_id=1,
        )
        sess.add(draft)
        sess.flush()
        sess.add(
            AgentReport(
                user_id="ariel",
                agent_role="fund_manager",
                decision_id="plan-synth-1",
                response_text=json.dumps(
                    {
                        "approved": False,
                        "reasons": [
                            "TOPIC_A — detail one",
                            "TOPIC_B — detail two",
                        ],
                        "cited_sources": [],
                    }
                ),
                model="claude-opus-4-7",
            )
        )
        sess.commit()
    finally:
        sess.close()

    # Make every translator call fail; the route must still return 200
    # with objections present and translation=None on each.
    async def _always_fails(self, *, topic, detail, severity, cited_sources=None):
        raise AgentRunError("nope")

    with patch(
        "argosy.agents.objection_translator.ObjectionTranslatorAgent.run",
        new=_always_fails,
    ):
        r = client_with_db.get("/api/plan/draft/objections?user_id=ariel")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approved"] is False
    assert len(body["objections"]) == 2
    for o in body["objections"]:
        assert o.get("translation") is None


def test_endpoint_attaches_translations_on_success(client_with_db):
    """End-to-end: successful translator runs surface as inline
    ``translation`` fields on each FMObjection in the response.
    """
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.flush()
        sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown="# Plan"))
        draft = PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="t",
            raw_markdown="",
            decision_run_id=2,
        )
        sess.add(draft)
        sess.flush()
        sess.add(
            AgentReport(
                user_id="ariel",
                agent_role="fund_manager",
                decision_id="plan-synth-2",
                response_text=json.dumps(
                    {
                        "approved": False,
                        "reasons": ["X_TOPIC — detail X"],
                        "cited_sources": [],
                    }
                ),
                model="claude-opus-4-7",
            )
        )
        sess.commit()
    finally:
        sess.close()

    async def _fake_run(self, *, topic, detail, severity, cited_sources=None):
        return _make_fake_report(
            headline=f"H::{topic}",
            plain_english=f"PE::{topic}",
            actions=[f"A::{topic}"],
        )

    with patch(
        "argosy.agents.objection_translator.ObjectionTranslatorAgent.run",
        new=_fake_run,
    ):
        r = client_with_db.get("/api/plan/draft/objections?user_id=ariel")

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["objections"]) == 1
    tr = body["objections"][0]["translation"]
    assert tr is not None
    assert tr["headline"] == "H::X_TOPIC"
    assert tr["plain_english"] == "PE::X_TOPIC"
    assert tr["recommended_actions"] == ["A::X_TOPIC"]


def test_empty_objections_returns_empty_dict(session):
    """Edge case: zero objections produces zero translator calls."""
    draft_id = session._draft_id
    calls: list[str] = []

    async def _fake_run(self, **kw):
        calls.append("called")
        return _make_fake_report("h", "pe", [])

    with patch(
        "argosy.agents.objection_translator.ObjectionTranslatorAgent.run",
        new=_fake_run,
    ):
        out = get_or_compute_translations(
            session,
            user_id="ariel",
            plan_version_id=draft_id,
            objections=[],
        )

    assert out == {}
    assert calls == []
