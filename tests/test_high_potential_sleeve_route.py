"""Route tests for GET /api/portfolio/high-potential-sleeve (S18)."""
from __future__ import annotations


def test_sleeve_route_returns_conviction_weighted_blend(client_with_db):
    r = client_with_db.get(
        "/api/portfolio/high-potential-sleeve",
        params={"cash_usd": 250_000, "sleeve_pct": 5.0},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sleeve_budget_usd"] == 12_500.0
    assert body["candidates"], "expected seed candidates"
    # Dollars sum to the sleeve budget.
    assert abs(sum(c["amount_usd"] for c in body["candidates"]) - 12_500.0) < 1.0
    # Blend present: both vehicles, UCITS core is the majority, single-names US-situs.
    split = body["vehicle_split"]
    assert split["ucits_thematic"] >= split["single_name"]
    for c in body["candidates"]:
        if c["vehicle"] == "single_name":
            assert c["us_situs"] is True
        else:
            assert c["us_situs"] is False
        assert c["thesis"].strip()


def test_sleeve_route_scales_with_cash_and_pct(client_with_db):
    r = client_with_db.get(
        "/api/portfolio/high-potential-sleeve",
        params={"cash_usd": 100_000, "sleeve_pct": 10.0},
    )
    assert r.status_code == 200, r.text
    assert r.json()["sleeve_budget_usd"] == 10_000.0
