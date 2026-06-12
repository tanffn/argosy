"""GET /api/portfolio/deploy-cash (P1 + P2 live-context path)."""
from datetime import date, datetime, timezone

import pytest
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


def _make_fake_market_context():
    """Build a minimal DeploymentMarketContext for monkeypatching."""
    from argosy.services.deployment_market_context import (
        DataFreshness, DeploymentMarketContext, NvdaVerification,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    freshness = (
        DataFreshness(field="sp500", fetched_at=now_iso, age_seconds=5.0,
                      source="fred", is_stale=False),
        DataFreshness(field="vix", fetched_at=now_iso, age_seconds=5.0,
                      source="fred", is_stale=False),
        DataFreshness(field="usd_nis", fetched_at=now_iso, age_seconds=3.0,
                      source="boi", is_stale=False),
    )
    nvda = NvdaVerification(price=135.0, shares=24.4e9, market_cap=3.3e12,
                            consistent=True, note="consistent: within 10%")
    return DeploymentMarketContext(
        snapshot={"sp500": 5500.0, "vix": 18.0, "usd_nis": 3.65,
                  "oil_wti": 78.0, "boi_rate": 4.5, "cpi_yoy": 3.2},
        freshness=freshness,
        nvda=nvda,
        overall_age_label="live",
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


# ---------------------------------------------------------------------------
# P2: live=true + live=false / omitted behaviour
# ---------------------------------------------------------------------------

def test_deploy_cash_live_true_returns_market_context(monkeypatch):
    """live=true: monkeypatched assembler → market_context block in response."""
    import argosy.api.routes.portfolio as portfolio
    import argosy.services.deployment_market_context as _dmc

    fake_ctx = _make_fake_market_context()

    monkeypatch.setattr(portfolio, "_load_current_doc_and_holdings",
                        lambda user_id: (_doc(), {}, 0.0))
    monkeypatch.setattr(_dmc, "assemble_deployment_market_context",
                        lambda session, **kwargs: fake_ctx)

    client = TestClient(create_app())
    resp = client.get("/api/portfolio/deploy-cash",
                      params={"cash_usd": 250000, "live": "true"})
    assert resp.status_code == 200
    body = resp.json()

    # market_context block must be present
    mc = body.get("market_context")
    assert mc is not None, "market_context should be present when live=true"

    # snapshot keys present
    assert "sp500" in mc["snapshot"]
    assert "vix" in mc["snapshot"]
    assert mc["snapshot"]["sp500"] == 5500.0

    # freshness list non-empty
    assert len(mc["freshness"]) >= 1
    first_f = mc["freshness"][0]
    assert "field" in first_f
    assert "age_seconds" in first_f
    assert "is_stale" in first_f

    # overall_age_label present
    assert mc["overall_age_label"] == "live"

    # nvda present and consistent
    assert mc["nvda"] is not None
    assert mc["nvda"]["price"] == 135.0
    assert mc["nvda"]["consistent"] is True

    # is_any_stale reflects data (all fresh in our stub)
    assert mc["is_any_stale"] is False

    # core plan fields still intact
    assert body["deploy_amount_usd"] == 250000.0


def test_deploy_cash_live_omitted_no_market_context(monkeypatch):
    """live omitted → market_context is null; assembler is NOT called."""
    import argosy.api.routes.portfolio as portfolio
    import argosy.services.deployment_market_context as _dmc

    called = []

    def _should_not_be_called(session, **kwargs):
        called.append(True)
        raise AssertionError("assemble_deployment_market_context must not be called when live=false")

    monkeypatch.setattr(portfolio, "_load_current_doc_and_holdings",
                        lambda user_id: (_doc(), {}, 0.0))
    monkeypatch.setattr(_dmc, "assemble_deployment_market_context",
                        _should_not_be_called)

    client = TestClient(create_app())
    resp = client.get("/api/portfolio/deploy-cash", params={"cash_usd": 10000})
    assert resp.status_code == 200
    body = resp.json()

    # market_context absent (null) — P1 behaviour preserved
    assert body.get("market_context") is None
    # assembler was never invoked
    assert called == [], "assembler should not be invoked when live is not set"


def test_deploy_cash_live_false_explicit_no_market_context(monkeypatch):
    """live=false explicitly → same as omitted; assembler NOT called."""
    import argosy.api.routes.portfolio as portfolio
    import argosy.services.deployment_market_context as _dmc

    called = []

    def _should_not_be_called(session, **kwargs):
        called.append(True)
        raise AssertionError("assembler must not be called when live=false")

    monkeypatch.setattr(portfolio, "_load_current_doc_and_holdings",
                        lambda user_id: (_doc(), {}, 0.0))
    monkeypatch.setattr(_dmc, "assemble_deployment_market_context",
                        _should_not_be_called)

    client = TestClient(create_app())
    resp = client.get("/api/portfolio/deploy-cash",
                      params={"cash_usd": 10000, "live": "false"})
    assert resp.status_code == 200
    assert resp.json().get("market_context") is None
    assert called == []
