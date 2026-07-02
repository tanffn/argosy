"""Daily FX-refresh cadence loop — keeps USD/NIS fresh for ALL consumers so no
surface computes on a stale rate."""
from __future__ import annotations

import asyncio

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import argosy.orchestrator.loops.fx_refresh_loop as mod
from argosy.orchestrator.loops.fx_refresh_loop import FxRefreshLoop
from argosy.state.models import Base


def _factory():
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_tick_refreshes_and_reports(monkeypatch):
    calls = {"n": 0}

    def _fake_refresh(session, **kw):
        calls["n"] += 1
        return True

    monkeypatch.setattr(mod, "refresh_if_stale", _fake_refresh)
    loop = FxRefreshLoop(session_factory=_factory(), user_id="ariel")

    result = asyncio.run(loop.tick())

    assert calls["n"] == 1
    assert result == {"refreshed": True}


def test_tick_isolates_failure(monkeypatch):
    def _boom(session, **kw):
        raise RuntimeError("BoI down")

    monkeypatch.setattr(mod, "refresh_if_stale", _boom)
    loop = FxRefreshLoop(session_factory=_factory(), user_id="ariel")

    result = asyncio.run(loop.tick())

    assert "error" in result
