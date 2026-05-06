"""Tests for plan_watcher daily cadence loop.

The loop hashes each user's configured plan source path. On hash change,
re-runs distillation (preserving user edits). Designed to be cheap when
nothing has changed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


def _write(p: Path, contents: str) -> None:
    p.write_text(contents, encoding="utf-8")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_plan_watcher_no_change_is_noop(session, tmp_path, monkeypatch):
    from argosy.orchestrator.loops import plan_watcher
    from argosy.services import plan_distiller_service as svc

    # Set up a baseline + matching source file.
    plan_path = tmp_path / "plan.md"
    contents = "# Plan\n\nNVDA target 15%\n"
    _write(plan_path, contents)

    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        version_label="Jacobs v2.0",
        source_path=str(plan_path),
        raw_markdown=contents,
        source_hash=_sha(contents),
    )
    session.add(pv)
    session.commit()

    # Spy: distill should NOT be called when nothing changed.
    calls = []
    monkeypatch.setattr(
        svc, "distill_baseline_plan",
        lambda **kw: calls.append(kw) or type("R", (), {"distillate": None, "source_hash": _sha(contents), "user_edits_preserved": 0, "plan_version_id": pv.id})(),
    )

    plan_watcher.tick(session)
    assert calls == [], "distillation should be skipped when source_hash matches"


def test_plan_watcher_redistills_on_file_change(session, tmp_path, monkeypatch):
    from argosy.orchestrator.loops import plan_watcher
    from argosy.services import plan_distiller_service as svc

    plan_path = tmp_path / "plan.md"
    _write(plan_path, "# Plan v1\n\nNVDA 15%\n")

    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        source_path=str(plan_path),
        raw_markdown="# Plan v1\n\nNVDA 15%\n",
        source_hash=_sha("# Plan v1\n\nNVDA 15%\n"),
    )
    session.add(pv)
    session.commit()

    # Mutate the file.
    _write(plan_path, "# Plan v2\n\nNVDA 12%\n")

    calls: list[dict] = []
    def _fake(**kw):
        calls.append(kw)
        # Simulate updating raw_markdown + source_hash inside the service.
        target = session.get(PlanVersion, kw["plan_version_id"])
        target.raw_markdown = plan_path.read_text(encoding="utf-8")
        target.source_hash = _sha(target.raw_markdown)
        session.commit()
        return type("R", (), {
            "distillate": None,
            "source_hash": target.source_hash,
            "user_edits_preserved": 0,
            "plan_version_id": kw["plan_version_id"],
        })()

    monkeypatch.setattr(svc, "distill_baseline_plan", _fake)

    plan_watcher.tick(session)
    assert len(calls) == 1
    assert calls[0]["preserve_user_edits"] is True


def test_plan_watcher_skips_users_without_source_path(session, monkeypatch):
    """If source_path is empty, the watcher cannot diff — skip silently."""
    from argosy.orchestrator.loops import plan_watcher
    from argosy.services import plan_distiller_service as svc

    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        source_path="",  # blank — uploaded via UI, no auto-watched file
        raw_markdown="# Plan",
        source_hash=_sha("# Plan"),
    )
    session.add(pv)
    session.commit()

    calls = []
    monkeypatch.setattr(svc, "distill_baseline_plan", lambda **kw: calls.append(kw))
    plan_watcher.tick(session)
    assert calls == []


def test_plan_watcher_handles_missing_file_gracefully(session, tmp_path, monkeypatch, caplog):
    """File deleted between ticks — log a warning, do not crash."""
    from argosy.orchestrator.loops import plan_watcher
    from argosy.services import plan_distiller_service as svc

    pv = PlanVersion(
        user_id="ariel",
        role="baseline",
        source_path=str(tmp_path / "deleted.md"),
        raw_markdown="# Plan",
        source_hash=_sha("# Plan"),
    )
    session.add(pv)
    session.commit()

    calls = []
    monkeypatch.setattr(svc, "distill_baseline_plan", lambda **kw: calls.append(kw))
    plan_watcher.tick(session)  # must not raise
    assert calls == []
