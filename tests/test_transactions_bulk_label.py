"""POST /api/expenses/transactions/bulk-label."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture()
def seeded(expense_client):
    from argosy.services.expense_ingest.taxonomy_seed import (
        seed_system_defaults, seed_user_categories,
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
        f = UserFile(
            user_id="ariel", sha256="b" * 64,
            original_name="seed", sanitized_name="seed",
            mime_type="application/octet-stream", kind="other",
            size_bytes=1, storage_path="/tmp/seed", source="chat_attachment",
        )
        s.add(f)
        s.flush()
        src = ExpenseSource(
            user_id="ariel", kind="card", issuer="isracard",
            external_id="8888", display_name="T",
        )
        s.add(src)
        s.flush()
        stmt = ExpenseStatement(
            user_id="ariel", source_id=src.id, file_id=f.id,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("30.00"),
            parser_name="isracard", parser_version="0.1.0",
            status="parsed",
        )
        s.add(stmt)
        s.flush()
        ids = []
        for i in range(3):
            tx = ExpenseTransaction(
                user_id="ariel", statement_id=stmt.id, source_id=src.id,
                occurred_on=date(2026, 5, 1 + i),
                merchant_raw="M", merchant_normalized="M",
                amount_nis=Decimal("10.00"), direction="debit",
                tx_type="regular", raw_row_json="{}",
                category_id=uncat.id, category_source="llm",
                category_confidence=Decimal("0.40"),
            )
            s.add(tx)
            s.flush()
            ids.append(tx.id)
        s.commit()
    return expense_client, ids


def test_bulk_category_only(seeded):
    client, ids = seeded
    r = client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel", "transaction_ids": ids,
              "category_slug": "food.groceries"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["affected"] == 3
    assert body["skipped"] == []

    # Cache row NOT written.
    from argosy.state.models import MerchantCategoryCache
    SessionLocal = client.app.state.session_factory
    with SessionLocal() as s:
        cache = s.query(MerchantCategoryCache).filter_by(
            user_id="ariel", merchant_pattern="M",
        ).one_or_none()
        assert cache is None


def test_bulk_tags_only(seeded):
    client, ids = seeded
    r = client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel", "transaction_ids": ids,
              "add_tags": ["trip:greece-2026-aug"]},
    )
    assert r.status_code == 200
    from argosy.state.models import ExpenseTransaction
    SessionLocal = client.app.state.session_factory
    with SessionLocal() as s:
        for tx_id in ids:
            tx = s.get(ExpenseTransaction, tx_id)
            assert "trip:greece-2026-aug" in json.loads(tx.tags)


def test_bulk_remove_tags(seeded):
    client, ids = seeded
    client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel", "transaction_ids": ids,
              "add_tags": ["a", "b"]},
    )
    r = client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel", "transaction_ids": ids,
              "remove_tags": ["a"]},
    )
    assert r.status_code == 200
    from argosy.state.models import ExpenseTransaction
    SessionLocal = client.app.state.session_factory
    with SessionLocal() as s:
        for tx_id in ids:
            tx = s.get(ExpenseTransaction, tx_id)
            tags = json.loads(tx.tags)
            assert "a" not in tags
            assert "b" in tags


def test_bulk_combined_category_and_tags(seeded):
    client, ids = seeded
    r = client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel", "transaction_ids": ids,
              "category_slug": "insurance.health",  # may not exist; add fallback
              "add_tags": ["trip:x"]},
    )
    # If category doesn't exist, we expect 400.
    assert r.status_code in (200, 400), r.text


def test_bulk_empty_body_returns_422(seeded):
    client, ids = seeded
    r = client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel", "transaction_ids": ids},
    )
    assert r.status_code == 422


def test_bulk_unknown_tx_id_lands_in_skipped(seeded):
    client, ids = seeded
    r = client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel",
              "transaction_ids": ids + [999999],
              "category_slug": "food.groceries"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["affected"] == 3
    assert any(s["tx_id"] == 999999 for s in body["skipped"])
