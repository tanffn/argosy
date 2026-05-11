"""PATCH /api/expenses/merchants/{merchant_normalized}."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture()
def seeded(expense_client):
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
            user_id="ariel", sha256="b" * 64,
            original_name="test.pdf", sanitized_name="test.pdf",
            mime_type="application/pdf", kind="other",
            size_bytes=1, storage_path="/tmp/test.pdf",
            source="chat_attachment",
        )
        s.add(uf); s.flush()
        src = ExpenseSource(user_id="ariel", kind="card", issuer="isracard",
                            external_id="5555", display_name="T")
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
        for i in range(2):
            s.add(ExpenseTransaction(
                user_id="ariel", statement_id=stmt.id, source_id=src.id,
                occurred_on=date(2026, 5, 1 + i),
                merchant_raw="שטראוס", merchant_normalized="שטראוס",
                amount_nis=Decimal("10.00"), direction="debit",
                tx_type="regular", raw_row_json="{}",
                category_id=uncat.id, category_source="llm",
                category_confidence=Decimal("0.40"),
            ))
        s.commit()
    return expense_client


def test_patch_with_category_slug(seeded):
    import urllib.parse
    name = urllib.parse.quote("שטראוס")
    r = seeded.patch(
        f"/api/expenses/merchants/{name}",
        json={"user_id": "ariel", "category_slug": "food.groceries"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["affected_transactions"] == 2
    assert body["cache_row_created"] is True
    assert body["category_slug"] == "food.groceries"


def test_patch_confirm_only_uses_current_category(seeded):
    # First categorize via PATCH (creates cache row).
    import urllib.parse
    name = urllib.parse.quote("שטראוס")
    seeded.patch(
        f"/api/expenses/merchants/{name}",
        json={"user_id": "ariel", "category_slug": "food.groceries"},
    )
    # Then confirm — category unchanged.
    r = seeded.patch(
        f"/api/expenses/merchants/{name}",
        json={"user_id": "ariel", "confirm": True},
    )
    assert r.status_code == 200
    assert r.json()["category_slug"] == "food.groceries"


def test_patch_unknown_merchant_returns_404(seeded):
    r = seeded.patch(
        "/api/expenses/merchants/no-such-merchant",
        json={"user_id": "ariel", "category_slug": "food.groceries"},
    )
    assert r.status_code == 404


def test_patch_unknown_category_returns_400(seeded):
    import urllib.parse
    name = urllib.parse.quote("שטראוס")
    r = seeded.patch(
        f"/api/expenses/merchants/{name}",
        json={"user_id": "ariel", "category_slug": "no-such-category"},
    )
    assert r.status_code == 400


def test_patch_missing_both_fields_returns_422(seeded):
    import urllib.parse
    name = urllib.parse.quote("שטראוס")
    r = seeded.patch(
        f"/api/expenses/merchants/{name}",
        json={"user_id": "ariel"},
    )
    assert r.status_code == 422
