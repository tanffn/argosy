"""HTTP route tests for /api/expenses/*."""

from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"



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
    """Card-payment rows must NOT contribute to per-currency totals.

    Since T14 the response is a list of {month, totals_by_currency,
    transaction_count} entries; this test pins the per-currency shape and
    re-asserts that is_card_payment rows are not summed into totals.
    """
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
    assert isinstance(body, list)
    assert len(body) >= 1
    for entry in body:
        assert "month" in entry
        assert "totals_by_currency" in entry
        assert isinstance(entry["totals_by_currency"], dict)
        assert "transaction_count" in entry


def test_list_transactions_passes_through_foreign_amount_nis_null(client_with_db):
    """Post-EX1.1, foreign rows have amount_nis=None. The endpoint must surface
    that without TypeError'ing through float()."""
    from datetime import date
    from decimal import Decimal
    from sqlalchemy.orm import Session
    from argosy.state.models import (
        ExpenseSource, ExpenseStatement, ExpenseTransaction, User, UserFile,
    )

    SessionFactory = client_with_db.app.state.session_factory
    with SessionFactory() as s:
        s.add(User(id="u_fx", plan="free")); s.flush()
        f = UserFile(
            user_id="u_fx", sha256="f"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id="u_fx", kind="card", issuer="isracard",
            external_id="0000", display_name="test",
        )
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id="u_fx", source_id=src.id, file_id=f.id,
            period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
            parsed_total_nis=Decimal("0"), parser_name="isracard",
            parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        # Foreign row with amount_nis NULL.
        s.add(ExpenseTransaction(
            user_id="u_fx", source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 4, 5),
            merchant_raw="NETFLIX", merchant_normalized="netflix",
            amount_nis=None,
            amount_orig=Decimal("12.18"), currency_orig="USD",
            direction="debit", tx_type="regular", raw_row_json="{}",
        ))
        s.commit()

    r = client_with_db.get("/api/expenses/transactions?user_id=u_fx")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    tx = body["transactions"][0]
    assert tx["amount_nis"] is None
    assert tx["amount_orig"] == 12.18
    assert tx["currency_orig"] == "USD"
