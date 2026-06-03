"""Narrative DB-persistence (migration 0062).

The bilingual plan narrative is written through to
``plan_versions.narrative_json`` on first generation and read back from
there on a cache miss — so it survives a backend restart and loads
instantly instead of re-running the LLM.
"""
from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import argosy.services.plan_narrative as svc
from argosy.state.models import Base, PlanVersion, User


@pytest.fixture
def db():
    engine = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clear_cache():
    svc._CACHE.clear()
    yield
    svc._CACHE.clear()


def _current(s, **kw) -> PlanVersion:
    pv = PlanVersion(user_id="ariel", role="current", version_label="cur",
                     horizon_long_md="# L", **kw)
    s.add(pv)
    s.commit()
    return pv


async def test_persisted_narrative_served_without_running_agent(db, monkeypatch):
    """A current plan with a populated narrative_json returns instantly
    and NEVER invokes the (expensive) PlanNarrativeAgent."""
    _current(db, narrative_json=json.dumps({
        "narrative_md_en": "EN body", "narrative_md_he": "HE body",
        "confidence": "HIGH",
    }))

    def _boom(*a, **k):  # pragma: no cover - asserts it isn't called
        raise AssertionError("agent must not run when narrative is persisted")

    monkeypatch.setattr(
        "argosy.agents.plan_narrative.PlanNarrativeAgent", _boom
    )

    res = await svc.get_plan_narrative(db, "ariel")
    assert res is not None
    assert res.narrative_md_en == "EN body"
    assert res.narrative_md_he == "HE body"
    assert res.confidence == "HIGH"


async def test_generates_then_writes_through_when_absent(db, monkeypatch):
    """When narrative_json is NULL the agent runs once, and the result is
    persisted to the column (write-through) so the next load is free."""
    pv = _current(db)
    assert pv.narrative_json is None

    class _Out:
        narrative_md_en = "gen EN"
        narrative_md_he = "gen HE"
        confidence = "MEDIUM"

    class _Report:
        output = _Out()

    class _StubAgent:
        def __init__(self, *, user_id):
            pass

        async def run(self, **kw):
            return _Report()

    monkeypatch.setattr(
        "argosy.agents.plan_narrative.PlanNarrativeAgent", _StubAgent
    )

    res = await svc.get_plan_narrative(db, "ariel")
    assert res.narrative_md_en == "gen EN"

    # Write-through persisted it onto the row.
    db.refresh(pv)
    assert pv.narrative_json is not None
    data = json.loads(pv.narrative_json)
    assert data["narrative_md_en"] == "gen EN"
    assert data["confidence"] == "MEDIUM"
