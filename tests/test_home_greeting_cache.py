"""Home greeting dirty-flag + bake (§7.2)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services import home_greeting_cache as hgc
from argosy.state.models import Base, KvCacheEntry, User

_NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'greeting.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


def test_visit_bakes_and_serves_until_dirty(db, monkeypatch):
    calls: list[int] = []

    def fake_build(session, user_id, *, now=None):
        calls.append(1)
        return {
            "greeting_name": "Ariel",
            "book": {
                "total_usd": 1.0,
                "on_plan": True,
                "on_plan_note": f"build-{len(calls)}",
                "fi_line": "FI",
                "as_of": None,
            },
            "needs_you": [],
            "watching": [],
            "quiet": True,
            "next_review_local": "17:00",
        }

    monkeypatch.setattr(
        "argosy.services.home_greeting.build_greeting", fake_build
    )

    first = hgc.get_or_refresh_greeting(db, "ariel", now=_NOW)
    assert first["book"]["on_plan_note"] == "build-1"
    assert len(calls) == 1

    second = hgc.get_or_refresh_greeting(db, "ariel", now=_NOW)
    assert second["book"]["on_plan_note"] == "build-1"
    assert len(calls) == 1  # bake hit

    # Plan-promote path: dirty mark → next visit regenerates.
    hgc.mark_home_greeting_dirty("ariel", session=db, commit=True)

    third = hgc.get_or_refresh_greeting(db, "ariel", now=_NOW)
    assert third["book"]["on_plan_note"] == "build-2"
    assert len(calls) == 2


def test_force_rebuilds(db, monkeypatch):
    n = {"c": 0}

    def fake_build(session, user_id, *, now=None):
        n["c"] += 1
        return {
            "greeting_name": "Ariel",
            "book": {
                "total_usd": None,
                "on_plan": False,
                "on_plan_note": f"n{n['c']}",
                "fi_line": "",
                "as_of": None,
            },
            "needs_you": [],
            "watching": [],
            "quiet": True,
            "next_review_local": None,
        }

    monkeypatch.setattr(
        "argosy.services.home_greeting.build_greeting", fake_build
    )
    hgc.get_or_refresh_greeting(db, "ariel", now=_NOW)
    forced = hgc.get_or_refresh_greeting(db, "ariel", now=_NOW, force=True)
    assert forced["book"]["on_plan_note"] == "n2"


def test_invalidate_home_brief_also_marks_greeting_dirty(db, monkeypatch):
    """Plan promote path calls invalidate_home_brief — must dirty greeting."""
    from argosy.adapters.data import cache as cache_mod

    dirtied: list[str] = []
    monkeypatch.setattr(
        cache_mod, "purge_cache_entry", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hgc, "mark_home_greeting_dirty", lambda uid: dirtied.append(uid)
    )
    # Re-bind the import path used inside invalidate_home_brief
    import argosy.services.home_greeting_cache as hgc_mod

    monkeypatch.setattr(
        hgc_mod, "mark_home_greeting_dirty", lambda uid: dirtied.append(uid)
    )

    cache_mod.invalidate_home_brief("ariel")
    assert dirtied == ["ariel"]
