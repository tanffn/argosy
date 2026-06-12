"""Route test: /api/portfolio/allocation-tasks reads the canonical plan."""
from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

import argosy.api.routes.portfolio as portfolio_routes
from argosy.api.main import create_app


def _doc():
    from argosy.services.target_allocation_doc import (
        TargetAllocationDoc, AllocationClassDoc, AllocationInstrument, GlideWaypoint,
    )
    return TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="test",
        classes=[AllocationClassDoc(label="Core", snapshot_category="Core", sigma_class="us_equity",
                 target_pct=100.0, instruments=[AllocationInstrument(symbol="CSPX", role="primary",
                 weight_within_class_pct=100.0, domicile="IE")])],
        glide=[GlideWaypoint(quarter=0, date=date(2026, 1, 1), composition_pct_by_class={"Core": 100.0})],
    )


def test_allocation_tasks_cash_deploy(monkeypatch):
    monkeypatch.setattr(portfolio_routes, "_load_current_doc_and_holdings",
                        lambda user_id: (_doc(), {"CSPX": 1000.0}, 0.0))
    client = TestClient(create_app())
    r = client.get("/api/portfolio/allocation-tasks",
                   params={"mode": "cash_only_deploy", "cash_usd": 500, "user_id": "ariel"})
    assert r.status_code == 200
    body = r.json()
    assert body["candidates"]
    assert all(leg["side"] == "BUY" for c in body["candidates"] for leg in c["legs"])


def test_allocation_tasks_no_plan_returns_empty_with_note(monkeypatch):
    monkeypatch.setattr(portfolio_routes, "_load_current_doc_and_holdings",
                        lambda user_id: (None, {}, 0.0))
    client = TestClient(create_app())
    r = client.get("/api/portfolio/allocation-tasks", params={"cash_usd": 500})
    assert r.status_code == 200
    body = r.json()
    assert body["candidates"] == []
    assert "plan" in body["note"].lower()


def test_allocation_tasks_malformed_plan_fails_loud(monkeypatch):
    """A non-conserving plan (class pct != ~100) must surface as an error note,
    never silently produce a mis-sized allocation (fail loud)."""
    from argosy.services.target_allocation_doc import (
        TargetAllocationDoc, AllocationClassDoc, AllocationInstrument, GlideWaypoint,
    )
    bad = TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="t",
        classes=[AllocationClassDoc(label="Core", snapshot_category="Core", sigma_class="us_equity",
                 target_pct=100.0, instruments=[AllocationInstrument(symbol="CSPX", role="primary",
                 weight_within_class_pct=100.0, domicile="IE")])],
        glide=[GlideWaypoint(quarter=0, date=date(2026, 1, 1), composition_pct_by_class={"Core": 70.0})],
    )
    monkeypatch.setattr(portfolio_routes, "_load_current_doc_and_holdings",
                        lambda user_id: (bad, {}, 0.0))
    client = TestClient(create_app())
    r = client.get("/api/portfolio/allocation-tasks", params={"cash_usd": 500})
    assert r.status_code == 200
    body = r.json()
    assert body["candidates"] == []
    assert "could not" in body["note"].lower() or "error" in body["note"].lower()
