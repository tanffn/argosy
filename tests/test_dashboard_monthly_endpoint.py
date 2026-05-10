"""Tests for GET /api/expenses/dashboard-monthly.

The endpoint is the Monthly tab's data source. It bundles:

  - month (echo) + available_months (for the month picker)
  - chart_window (always exactly 12 bars, padding-aware)
  - hero_stats (spent/income/refunds with MoM + trailing-12 deltas)
  - top_categories, categories_vs_typical, top_merchants
  - largest_transactions (top 5 by |amount_nis|)
  - anomalies (uncategorized, conservation_gap, fee_waiver_missed,
    merchant_spike, new_high_value_merchant)
"""

from __future__ import annotations


def test_dashboard_monthly_basic_shape(client_with_seeded_data):
    """Note: client_with_seeded_data is a tuple of (TestClient, user_id, month)
    with a recent month worth of data."""
    client, user_id, month = client_with_seeded_data
    r = client.get(
        f"/api/expenses/dashboard-monthly?user_id={user_id}&month={month}"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["month"] == month
    assert isinstance(body["available_months"], list)
    assert len(body["chart_window"]) == 12
    assert any(b["is_selected"] for b in body["chart_window"])
    assert "spent" in body["hero_stats"]
    assert "income" in body["hero_stats"]
    assert "refunds" in body["hero_stats"]
    assert isinstance(body["top_categories"], list)
    assert isinstance(body["categories_vs_typical"], list)
    assert len(body["categories_vs_typical"]) <= 3
    assert isinstance(body["largest_transactions"], list)
    assert len(body["largest_transactions"]) <= 5
    assert isinstance(body["anomalies"], list)


def test_dashboard_monthly_padding_for_short_history(client_with_short_history):
    client, user_id, month = client_with_short_history
    r = client.get(
        f"/api/expenses/dashboard-monthly?user_id={user_id}&month={month}"
    )
    assert r.status_code == 200
    body = r.json()
    pad_count = sum(1 for b in body["chart_window"] if b["is_padding"])
    assert pad_count > 0
    assert len(body["chart_window"]) == 12


def test_dashboard_monthly_missing_month_param(client_with_seeded_data):
    client, user_id, month = client_with_seeded_data
    r = client.get(f"/api/expenses/dashboard-monthly?user_id={user_id}")
    assert r.status_code == 422    # FastAPI validation error
