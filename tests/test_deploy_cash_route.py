"""GET /api/portfolio/deploy-cash (P1)."""
from datetime import date

from fastapi.testclient import TestClient

from argosy.api.main import create_app


def _doc():
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc, AllocationInstrument, TargetAllocationDoc,
    )
    return TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.16, nvda_cap_pct=13.0, fi_pct=10.0,
        provenance="test",
        classes=[AllocationClassDoc(
            label="US broad-market core", snapshot_category="Core Equity",
            sigma_class="us_equity", target_pct=100.0,
            instruments=[AllocationInstrument(symbol="CSPX", role="primary",
                                              weight_within_class_pct=100.0,
                                              rationale="", domicile="IE")],
            agreement="", rationale="", dissent="")],
        glide=[],
    )


def test_deploy_cash_returns_tiered_plan(monkeypatch):
    import argosy.api.routes.portfolio as portfolio
    monkeypatch.setattr(portfolio, "_load_current_doc_and_holdings",
                        lambda user_id: (_doc(), {}, 0.0))
    client = TestClient(create_app())
    resp = client.get("/api/portfolio/deploy-cash", params={"cash_usd": 10000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["deploy_amount_usd"] == 10000.0
    assert {t["name"] for t in body["tiers"]} == {"reserve", "core", "medium", "high"}
    core = next(t for t in body["tiers"] if t["name"] == "core")
    assert core["lines"][0]["symbol"] == "CSPX"
    assert core["lines"][0]["estate"]["status"] == "estate_safe"


def test_deploy_cash_no_plan_returns_empty_with_note(monkeypatch):
    import argosy.api.routes.portfolio as portfolio
    monkeypatch.setattr(portfolio, "_load_current_doc_and_holdings",
                        lambda user_id: (None, {}, 0.0))
    client = TestClient(create_app())
    resp = client.get("/api/portfolio/deploy-cash", params={"cash_usd": 10000})
    assert resp.status_code == 200
    assert "plan" in resp.json()["note"].lower()
