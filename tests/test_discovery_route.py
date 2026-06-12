"""Phase 2 — combined discovery endpoint (codex #12: NEW DTO, old sleeve kept)
+ the separate DiscoveryFunnelLoop (codex #10)."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

import argosy.api.routes.portfolio as portfolio_routes
from argosy.api.main import create_app
from argosy.services.contracts import EstimatorVerdict, FleetPick
from argosy.services.high_potential_funnel import FunnelResult


def test_get_discovery_reads_cached_state(monkeypatch):
    picks = [FleetPick(ticker="PLTR", conviction="HIGH", thesis_md="AI platform",
                       verdict="BUY", cites=("fundamentals",))]
    est = [EstimatorVerdict(ticker="PLTR", go=True, conviction="HIGH",
                            sentiment=0.8, one_line="go")]
    monkeypatch.setattr(portfolio_routes, "_load_discovery_state",
                        lambda user_id: (picks, est, "2026-06-12T12:00:00+00:00"))
    client = TestClient(create_app())
    r = client.get("/api/portfolio/discovery", params={"user_id": "ariel"})
    assert r.status_code == 200
    body = r.json()
    assert body["picks"][0]["ticker"] == "PLTR"
    assert body["picks"][0]["verdict"] == "BUY"
    assert body["last_refreshed_at"] == "2026-06-12T12:00:00+00:00"
    # conviction-only: no dollar amounts on the discovery surface
    assert "amount_usd" not in body["picks"][0]


def test_post_refresh_runs_funnel(monkeypatch):
    import argosy.services.high_potential_funnel as hpf

    async def fake_run(user_id, *, force=False, now=None):
        return FunnelResult(
            picks=[FleetPick(ticker="NVDA", conviction="HIGH", thesis_md="x",
                             verdict="BUY", cites=())],
            estimated=[EstimatorVerdict(ticker="NVDA", go=True, conviction="HIGH",
                                        sentiment=0.9, one_line="go")],
            radar=[], last_refreshed_at="2026-06-12T13:00:00+00:00")
    monkeypatch.setattr(hpf, "run_funnel", fake_run)
    client = TestClient(create_app())
    r = client.post("/api/portfolio/discovery/refresh",
                    params={"user_id": "ariel", "force": "true"})
    assert r.status_code == 200
    body = r.json()
    assert [p["ticker"] for p in body["picks"]] == ["NVDA"]
    assert body["last_refreshed_at"] == "2026-06-12T13:00:00+00:00"


def test_discovery_funnel_loop_tick_runs_funnel(monkeypatch):
    import argosy.orchestrator.loops.discovery_funnel_loop as dfl

    called = {}

    async def fake_run(user_id, *, force=False, now=None):
        called["user_id"] = user_id
        called["force"] = force
        return FunnelResult(picks=[FleetPick("PLTR", "HIGH", "t", "BUY", ())],
                            estimated=[], radar=[], last_refreshed_at="t")
    monkeypatch.setattr(dfl, "run_funnel", fake_run)
    loop = dfl.DiscoveryFunnelLoop(user_id="ariel")
    out = asyncio.run(loop.tick())
    assert called["user_id"] == "ariel"
    assert called["force"] is False           # daily refresh is SMART (not force)
    assert out["picks"] == 1
