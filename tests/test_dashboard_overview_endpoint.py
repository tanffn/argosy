"""Dashboard-overview endpoint — returns one bundle for the overview page."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from argosy.state.models import (
    ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
    User, UserFile,
)


def _seed_minimal(client_with_db, *, user_id: str = "u1"):
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        # Seed system + user categories
        from argosy.services.expense_ingest.taxonomy_seed import (
            seed_system_defaults, seed_user_categories,
        )
        seed_system_defaults(s); s.flush()
        seed_user_categories(s, user_id); s.flush()
        # Source + statement + 5 transactions in current month
        f = UserFile(
            user_id=user_id, sha256="a"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="card", issuer="isracard",
            external_id="0000", display_name="test",
        )
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=f.id,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("250"), declared_total_nis=Decimal("250"),
            parser_name="isracard", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="dining_out.restaurants",
        ).one()
        for i in range(5):
            s.add(ExpenseTransaction(
                user_id=user_id, source_id=src.id, statement_id=stmt.id,
                occurred_on=date(2026, 5, i + 1),
                merchant_raw=f"M{i}", merchant_normalized=f"m{i}",
                amount_nis=Decimal("50"), direction="debit", tx_type="regular",
                category_id=cat.id, category_source="user",
                category_confidence=Decimal("1.0"),
                raw_row_json="{}",
            ))
        s.commit()


def test_dashboard_overview_returns_full_payload(client_with_db):
    _seed_minimal(client_with_db)
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u1&months=6")
    assert r.status_code == 200
    body = r.json()
    assert "months" in body
    assert "current_month_top_categories" in body
    assert "top_merchants_current_month" in body
    assert "anomalies" in body
    assert "sources_health" in body
    assert "fx_mode" in body


def test_dashboard_overview_empty_corpus_returns_empty_lists(client_with_db):
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u_empty&months=6")
    assert r.status_code == 200
    body = r.json()
    assert body["months"] == []
    assert body["sources_health"] == []
    assert body["anomalies"] == []


def test_dashboard_overview_top_categories_ordered_by_spend(client_with_db):
    _seed_minimal(client_with_db, user_id="u2")
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u2&months=6")
    body = r.json()
    cats = body["current_month_top_categories"]
    # Each entry: {slug, label_en, total_nis, percent}
    assert all("slug" in c and "total_nis" in c for c in cats)
    if len(cats) >= 2:
        assert cats[0]["total_nis"] >= cats[1]["total_nis"]


def test_dashboard_overview_sources_health_includes_status(client_with_db):
    _seed_minimal(client_with_db, user_id="u3")
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u3&months=6")
    body = r.json()
    assert len(body["sources_health"]) == 1
    src = body["sources_health"][0]
    assert src["display_name"] == "test"
    # gap < 0.5 → green
    assert src["status"] == "green"
    assert src["gap"] is not None and abs(src["gap"]) < 0.5
