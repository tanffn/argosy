"""POST /api/expenses/merchants/bulk-category."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture()
def seeded_two_merchants(expense_client):
    from argosy.services.expense_ingest.taxonomy_seed import (
        seed_system_defaults,
        seed_user_categories,
    )
    from argosy.state.models import (
        ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
        UserFile,
    )
    SessionLocal = expense_client.app.state.session_factory
    with SessionLocal() as s:
        seed_system_defaults(s)
        s.flush()
        seed_user_categories(s, "ariel")
        s.flush()
        uncat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="uncategorized"
        ).one()
        uf = UserFile(
            user_id="ariel", sha256="c" * 64,
            original_name="bulk_test.pdf", sanitized_name="bulk_test.pdf",
            mime_type="application/pdf", kind="other",
            size_bytes=1, storage_path="/tmp/bulk_test.pdf",
            source="chat_attachment",
        )
        s.add(uf); s.flush()
        src = ExpenseSource(user_id="ariel", kind="card", issuer="isracard",
                            external_id="7777", display_name="T")
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id="ariel", source_id=src.id, file_id=uf.id,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("20.00"),
            parser_name="test", parser_version="0.1",
            status="parsed",
        )
        s.add(stmt); s.flush()
        for merch in ("A", "B"):
            s.add(ExpenseTransaction(
                user_id="ariel", statement_id=stmt.id, source_id=src.id,
                occurred_on=date(2026, 5, 1),
                merchant_raw=merch, merchant_normalized=merch,
                amount_nis=Decimal("10.00"), direction="debit",
                tx_type="regular", raw_row_json="{}",
                category_id=uncat.id, category_source="llm",
                category_confidence=Decimal("0.40"),
            ))
        s.commit()
    return expense_client


def test_bulk_happy_path(seeded_two_merchants):
    r = seeded_two_merchants.post(
        "/api/expenses/merchants/bulk-category",
        json={"user_id": "ariel", "merchant_normalizeds": ["A", "B"],
              "category_slug": "food.groceries"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok_count"] == 2
    assert body["error_count"] == 0
    assert body["total_affected_transactions"] == 2


def test_bulk_with_missing_merchant_surfaces_in_results(seeded_two_merchants):
    r = seeded_two_merchants.post(
        "/api/expenses/merchants/bulk-category",
        json={"user_id": "ariel",
              "merchant_normalizeds": ["A", "ghost"],
              "category_slug": "food.groceries"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok_count"] == 1
    assert body["error_count"] == 1
    statuses = {r_["merchant_normalized"]: r_["status"] for r_ in body["results"]}
    assert statuses == {"A": "ok", "ghost": "error"}


def test_bulk_requires_one_of_slug_or_confirm(seeded_two_merchants):
    r = seeded_two_merchants.post(
        "/api/expenses/merchants/bulk-category",
        json={"user_id": "ariel", "merchant_normalizeds": ["A"]},
    )
    assert r.status_code == 422
