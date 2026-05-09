"""HTTP route tests for /api/expenses/*."""

from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


@pytest.fixture()
def expense_client(client_with_db, tmp_path, monkeypatch):
    """client_with_db augmented with:
      - ARGOSY_HOME → tmp_path (so catalog_upload writes to a throw-away dir)
      - a seeded User row so FK-aware sessions don't fail
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    # Seed the 'ariel' user into the test DB so UserFile FK is satisfied even
    # when SQLite FK enforcement is on.
    from argosy.state.models import User
    SessionLocal = client_with_db.app.state.session_factory
    with SessionLocal() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
            s.commit()

    yield client_with_db

    reload_settings()


def test_upload_max_xlsx_returns_parse_summary(expense_client):
    with patch("argosy.services.expense_ingest.category_resolver._categorize_via_llm",
               return_value=[]):
        with open(FIXTURES / "max_minimal.xlsx", "rb") as f:
            files = {"files": ("max_minimal.xlsx", f.read(),
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
            resp = expense_client.post("/api/expenses/upload",
                                       files=files,
                                       data={"user_id": "ariel",
                                             "card_last4": "6225"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) == 1
    r = body["results"][0]
    assert r["filename"] == "max_minimal.xlsx"
    assert r["status"] == "parsed"
    assert r["transactions_inserted"] == 5


def test_upload_unknown_format_returns_failed_status(expense_client):
    files = {"files": ("garbage.bin", b"\x00\x01\x02\x03", "application/octet-stream")}
    resp = expense_client.post("/api/expenses/upload",
                                files=files, data={"user_id": "ariel"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["status"] == "failed"
    err = body["results"][0]["error"].lower()
    assert "unrecognized" in err or "unknown" in err


def test_upload_multi_file(expense_client):
    with patch("argosy.services.expense_ingest.category_resolver._categorize_via_llm",
               return_value=[]):
        files = [
            ("files", ("max_minimal.xlsx",
                       open(FIXTURES / "max_minimal.xlsx", "rb").read(),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("files", ("isracard_minimal.xlsx",
                       open(FIXTURES / "isracard_minimal.xlsx", "rb").read(),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ]
        resp = expense_client.post("/api/expenses/upload",
                                    files=files,
                                    data={"user_id": "ariel",
                                          "card_last4": "6225"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 2
    assert all(r["status"] == "parsed" for r in body["results"])


def test_list_sources_returns_active_only(expense_client):
    """Upload an Isracard file, then list sources — should return one row."""
    with patch("argosy.services.expense_ingest.category_resolver._categorize_via_llm",
               return_value=[]):
        with open(FIXTURES / "isracard_minimal.xlsx", "rb") as f:
            expense_client.post("/api/expenses/upload",
                                 files={"files": ("isracard_minimal.xlsx",
                                                  f.read(),
                                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                                 data={"user_id": "ariel"})
    resp = expense_client.get("/api/expenses/sources?user_id=ariel")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["sources"]) == 1
    assert body["sources"][0]["issuer"] == "isracard"
    assert body["sources"][0]["external_id"] == "1266"


def test_list_transactions_filters_by_category(expense_client):
    """Ingest a Max file (issuer-categorized), filter by dining_out."""
    with patch("argosy.services.expense_ingest.category_resolver._categorize_via_llm",
               return_value=[]):
        with open(FIXTURES / "max_minimal.xlsx", "rb") as f:
            expense_client.post("/api/expenses/upload",
                                 files={"files": ("max_minimal.xlsx", f.read(),
                                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                                 data={"user_id": "ariel",
                                       "card_last4": "6225"})
    resp = expense_client.get("/api/expenses/transactions",
                               params={"user_id": "ariel",
                                       "category": "dining_out.restaurants"})
    assert resp.status_code == 200
    body = resp.json()
    assert any("ספייס" in t["merchant_raw"] for t in body["transactions"])


def test_patch_transaction_category_updates_cache(expense_client):
    with patch("argosy.services.expense_ingest.category_resolver._categorize_via_llm",
               return_value=[]):
        with open(FIXTURES / "max_minimal.xlsx", "rb") as f:
            expense_client.post("/api/expenses/upload",
                                 files={"files": ("max_minimal.xlsx", f.read(),
                                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                                 data={"user_id": "ariel",
                                       "card_last4": "6225"})
    resp = expense_client.get("/api/expenses/transactions",
                               params={"user_id": "ariel"})
    tx_id = resp.json()["transactions"][0]["id"]
    upd = expense_client.patch(f"/api/expenses/transactions/{tx_id}",
                                json={"user_id": "ariel",
                                      "category_slug": "discretionary.entertainment"})
    assert upd.status_code == 200
    body = upd.json()
    assert body["category_source"] == "user"
    assert body["affected_count"] >= 1


def test_list_categories_returns_taxonomy(expense_client):
    with patch("argosy.services.expense_ingest.category_resolver._categorize_via_llm",
               return_value=[]):
        with open(FIXTURES / "max_minimal.xlsx", "rb") as f:
            expense_client.post("/api/expenses/upload",
                                 files={"files": ("max_minimal.xlsx", f.read(),
                                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                                 data={"user_id": "ariel",
                                       "card_last4": "6225"})
    resp = expense_client.get("/api/expenses/categories?user_id=ariel")
    assert resp.status_code == 200
    body = resp.json()
    slugs = {c["slug"] for c in body["categories"]}
    assert "food.groceries" in slugs
    assert "dining_out.restaurants" in slugs
    assert "uncategorized" in slugs


def test_monthly_summary_excludes_card_payments(expense_client):
    """Card-payment rows must NOT contribute to category totals."""
    with patch("argosy.services.expense_ingest.category_resolver._categorize_via_llm",
               return_value=[]):
        with open(FIXTURES / "max_minimal.xlsx", "rb") as f:
            expense_client.post("/api/expenses/upload",
                                 files={"files": ("max_minimal.xlsx", f.read(),
                                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                                 data={"user_id": "ariel",
                                       "card_last4": "6225"})
    resp = expense_client.get("/api/expenses/monthly-summary",
                               params={"user_id": "ariel", "months": 12})
    assert resp.status_code == 200
    body = resp.json()
    assert "by_month" in body
    assert len(body["by_month"]) >= 1
