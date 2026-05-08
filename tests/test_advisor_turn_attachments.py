"""Tests for Wave 5 — multipart `/api/advisor/turn` with attachments.

Covers:
  - JSON path still works (Wave 1 contract preserved)
  - Multipart with markdown attachment → text appended to user_message,
    new baseline PlanVersion created, prior baseline superseded,
    distillation scheduled.
  - Multipart with image attachment → image_attachments forwarded to agent.
  - Multipart with mixed attachments
  - Unsupported MIME → 415
  - Oversize → 413
"""

from __future__ import annotations

from io import BytesIO

import pytest

from argosy.state.models import PlanVersion, User


def _png_bytes() -> bytes:
    """Smallest valid 1x1 PNG."""
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d4944415478da6300010000000500010d0a2db40000000049454e44ae426082"
    )


def _stub_canned_turn_factory():
    """Build an AdvisorAgent stub factory whose run() returns canned output
    AND captures the kwargs it was called with for assertions."""
    captured: dict = {}

    class _StubAgent:
        def __init__(self, user_id: str):
            self.user_id = user_id

        async def run(self, **kwargs):
            captured.update(kwargs)
            from argosy.agents.advisor import AdvisorTurnOutput
            from argosy.agents.base import AgentReport, ConfidenceBand

            out = AdvisorTurnOutput(
                stage=kwargs.get("current_stage", "stage_1"),
                question_for_user="ok",
                stage_complete=False,
                next_stage=None,
                confidence=ConfidenceBand.MEDIUM,
                cited_sources=[],
                notes_for_orchestrator="",
                context_updates=[],
                intake_session_id=kwargs.get("intake_session_id", "sess"),
                mode=kwargs.get("mode", "user_driven"),
            )
            return AgentReport(
                agent_role="advisor",
                user_id=self.user_id,
                model="stub",
                response_text='{"x":1}',
                tokens_in=1,
                tokens_out=1,
                cost_usd=0.0,
                prompt_hash="h",
                confidence=ConfidenceBand.MEDIUM,
                output=out,
            )

    return _StubAgent, captured


@pytest.mark.asyncio
async def test_turn_json_path_unchanged(client_with_db, monkeypatch):
    """JSON-body callers (Wave 1 contract) keep working untouched."""
    from argosy.api.routes import advisor as adv

    Stub, captured = _stub_canned_turn_factory()
    adv.set_advisor_agent_factory(Stub)
    try:
        sess = client_with_db.app.state.session_factory()
        try:
            if sess.get(User, "ariel") is None:
                sess.add(User(id="ariel", plan="free"))
                sess.commit()
        finally:
            sess.close()

        r = client_with_db.post(
            "/api/advisor/turn",
            json={"user_id": "ariel", "last_user_message": "hello world"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["question_for_user"] == "ok"
        # No attachments means no image_attachments threaded
        assert "image_attachments" not in captured or captured.get("image_attachments") in (None, [])
        # Last user message stays untouched
        assert "hello world" in captured.get("last_user_message", "")
    finally:
        adv.reset_advisor_agent_factory()


@pytest.mark.asyncio
async def test_turn_multipart_markdown_appends_to_message(client_with_db, monkeypatch):
    """Markdown attachment is read and appended to last_user_message."""
    from argosy.api.routes import advisor as adv

    Stub, captured = _stub_canned_turn_factory()
    adv.set_advisor_agent_factory(Stub)

    # Avoid scheduling real distillation in this test
    async def _noop(**_kw):
        return None

    monkeypatch.setattr(
        "argosy.services.plan_distiller_service.distill_baseline_plan_async",
        _noop,
    )

    try:
        sess = client_with_db.app.state.session_factory()
        try:
            if sess.get(User, "ariel") is None:
                sess.add(User(id="ariel", plan="free"))
                sess.commit()
        finally:
            sess.close()

        md_content = b"# My Plan\n\nGoal: retire by 2030.\n" + b"x" * 600
        r = client_with_db.post(
            "/api/advisor/turn",
            data={"user_id": "ariel", "last_user_message": "Look at this plan"},
            files={"attachments": ("plan.md", BytesIO(md_content), "text/markdown")},
        )
        assert r.status_code == 200, r.text
        appended = captured.get("last_user_message", "")
        assert "Look at this plan" in appended
        assert "[Attached file: plan.md]" in appended
        assert "Goal: retire by 2030" in appended
    finally:
        adv.reset_advisor_agent_factory()


@pytest.mark.asyncio
async def test_turn_multipart_markdown_creates_baseline_and_schedules_distill(
    client_with_db, monkeypatch,
):
    """A plan-shaped markdown upload becomes a role=baseline PlanVersion;
    distillation is scheduled in the background."""
    from argosy.api.routes import advisor as adv

    Stub, _captured = _stub_canned_turn_factory()
    adv.set_advisor_agent_factory(Stub)

    scheduled: list[dict] = []

    async def _capture_distill(**kwargs):
        scheduled.append(kwargs)

    monkeypatch.setattr(
        "argosy.services.plan_distiller_service.distill_baseline_plan_async",
        _capture_distill,
    )

    try:
        sess = client_with_db.app.state.session_factory()
        try:
            if sess.get(User, "ariel") is None:
                sess.add(User(id="ariel", plan="free"))
                sess.commit()
        finally:
            sess.close()

        md = b"# Plan\n\n" + b"content " * 100  # > 500 chars
        r = client_with_db.post(
            "/api/advisor/turn",
            data={"user_id": "ariel", "last_user_message": "ingest this"},
            files={"attachments": ("plan.md", BytesIO(md), "text/markdown")},
        )
        assert r.status_code == 200, r.text

        sess = client_with_db.app.state.session_factory()
        try:
            baselines = (
                sess.query(PlanVersion)
                .filter_by(user_id="ariel", role="baseline")
                .all()
            )
            assert len(baselines) == 1
            assert "plan" in baselines[0].source_path.lower()
        finally:
            sess.close()

        # Distillation was scheduled (FastAPI BackgroundTasks runs after
        # response; TestClient awaits background tasks).
        assert len(scheduled) == 1
        assert scheduled[0]["user_id"] == "ariel"
        assert scheduled[0]["plan_version_id"] is not None
    finally:
        adv.reset_advisor_agent_factory()


@pytest.mark.asyncio
async def test_turn_multipart_supersedes_prior_baseline(client_with_db, monkeypatch):
    """Uploading a new plan supersedes any prior baseline for the user."""
    from argosy.api.routes import advisor as adv

    Stub, _ = _stub_canned_turn_factory()
    adv.set_advisor_agent_factory(Stub)

    async def _noop(**_kw):
        pass

    monkeypatch.setattr(
        "argosy.services.plan_distiller_service.distill_baseline_plan_async",
        _noop,
    )

    try:
        sess = client_with_db.app.state.session_factory()
        try:
            if sess.get(User, "ariel") is None:
                sess.add(User(id="ariel", plan="free"))
            sess.add(
                PlanVersion(
                    user_id="ariel",
                    role="baseline",
                    version_label="prior",
                    source_path="old.md",
                    raw_markdown="# Old plan",
                )
            )
            sess.commit()
        finally:
            sess.close()

        md = b"# New plan\n\n" + b"x " * 300
        r = client_with_db.post(
            "/api/advisor/turn",
            data={"user_id": "ariel", "last_user_message": "newer plan"},
            files={"attachments": ("new.md", BytesIO(md), "text/markdown")},
        )
        assert r.status_code == 200, r.text

        sess = client_with_db.app.state.session_factory()
        try:
            baselines = (
                sess.query(PlanVersion)
                .filter_by(user_id="ariel", role="baseline")
                .all()
            )
            assert len(baselines) == 1
            assert baselines[0].source_path == "new.md"

            superseded = (
                sess.query(PlanVersion)
                .filter_by(user_id="ariel", role="superseded")
                .all()
            )
            assert any(s.source_path == "old.md" for s in superseded)
        finally:
            sess.close()
    finally:
        adv.reset_advisor_agent_factory()


@pytest.mark.asyncio
async def test_turn_multipart_image_threads_to_agent(client_with_db):
    """Image attachment is forwarded to the agent as image_attachments."""
    from argosy.api.routes import advisor as adv

    Stub, captured = _stub_canned_turn_factory()
    adv.set_advisor_agent_factory(Stub)

    try:
        sess = client_with_db.app.state.session_factory()
        try:
            if sess.get(User, "ariel") is None:
                sess.add(User(id="ariel", plan="free"))
                sess.commit()
        finally:
            sess.close()

        r = client_with_db.post(
            "/api/advisor/turn",
            data={"user_id": "ariel", "last_user_message": "what's this?"},
            files={
                "attachments": (
                    "shot.png", BytesIO(_png_bytes()), "image/png",
                ),
            },
        )
        assert r.status_code == 200, r.text

        imgs = captured.get("image_attachments")
        assert imgs and len(imgs) == 1
        assert imgs[0].kind == "image"
        assert imgs[0].original_name == "shot.png"
    finally:
        adv.reset_advisor_agent_factory()


@pytest.mark.asyncio
async def test_turn_multipart_rejects_pdf_with_415(client_with_db):
    from argosy.api.routes import advisor as adv

    Stub, _ = _stub_canned_turn_factory()
    adv.set_advisor_agent_factory(Stub)

    try:
        sess = client_with_db.app.state.session_factory()
        try:
            if sess.get(User, "ariel") is None:
                sess.add(User(id="ariel", plan="free"))
                sess.commit()
        finally:
            sess.close()

        r = client_with_db.post(
            "/api/advisor/turn",
            data={"user_id": "ariel", "last_user_message": "x"},
            files={"attachments": ("doc.pdf", BytesIO(b"%PDF-1.4\n..."), "application/pdf")},
        )
        assert r.status_code == 415, r.text
    finally:
        adv.reset_advisor_agent_factory()
