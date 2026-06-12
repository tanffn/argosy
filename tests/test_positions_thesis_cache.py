"""The /positions/thesis endpoint caches on (plan_version, snapshot): a repeat
request recomputes nothing and writes no reliability-ledger row (perf fix)."""
from __future__ import annotations

from fastapi.testclient import TestClient

import argosy.api.routes.positions as positions
from argosy.api.main import create_app


class _PV:
    id = 777
    decision_run_id = None


class _Snap:
    snapshot_date = "2026-06-12"
    positions = []
    total_usd_value_k = 1000.0


def _patch(monkeypatch, derive_calls, emit_calls):
    positions._THESIS_CACHE.clear()
    monkeypatch.setattr(positions, "get_pending_draft", lambda db, uid: _PV())
    monkeypatch.setattr(positions, "get_current_plan", lambda db, uid: _PV())
    monkeypatch.setattr(positions, "_load_portfolio_snapshot", lambda uid: _Snap())

    def fake_derive(**kwargs):
        derive_calls.append(1)
        return []  # empty theses -> _to_dto not exercised; cache stores []

    def fake_emit(*a, **k):
        emit_calls.append(1)

    monkeypatch.setattr(positions, "derive_position_theses", fake_derive)
    monkeypatch.setattr(positions, "emit_thesis_predictions", fake_emit)


def test_thesis_endpoint_caches_and_emits_once(monkeypatch):
    derive_calls: list[int] = []
    emit_calls: list[int] = []
    _patch(monkeypatch, derive_calls, emit_calls)
    client = TestClient(create_app())

    r1 = client.get("/api/positions/thesis", params={"user_id": "ariel"})
    r2 = client.get("/api/positions/thesis", params={"user_id": "ariel"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    # Second call served from cache: derive + ledger-emit each ran exactly once.
    assert len(derive_calls) == 1, f"derive ran {len(derive_calls)}x (expected 1)"
    assert len(emit_calls) == 1, f"emit ran {len(emit_calls)}x (expected 1; no write on cached read)"


def test_thesis_cache_misses_on_new_plan_version(monkeypatch):
    derive_calls: list[int] = []
    emit_calls: list[int] = []
    _patch(monkeypatch, derive_calls, emit_calls)
    client = TestClient(create_app())
    client.get("/api/positions/thesis", params={"user_id": "ariel"})

    class _PV2:
        id = 888  # plan changed -> new key -> recompute
        decision_run_id = None
    monkeypatch.setattr(positions, "get_pending_draft", lambda db, uid: _PV2())
    client.get("/api/positions/thesis", params={"user_id": "ariel"})
    assert len(derive_calls) == 2  # recomputed for the new plan version
