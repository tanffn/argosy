"""Regression: PATCH /transactions/{id} default fan-out vs explicit
apply_to_siblings flag."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture()
def seeded_client(expense_client):
    """expense_client + seeded category taxonomy + 3 txs for merchant 'X'."""
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
        uf = UserFile(
            user_id="ariel", sha256="f" * 64,
            original_name="test.pdf", sanitized_name="test.pdf",
            mime_type="application/pdf", kind="other",
            size_bytes=1, storage_path="/tmp/test.pdf",
            source="chat_attachment",
        )
        s.add(uf); s.flush()
        src = ExpenseSource(user_id="ariel", kind="card", issuer="isracard",
                            external_id="9999", display_name="Test")
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id="ariel", source_id=src.id, file_id=uf.id,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("30.00"),
            parser_name="test", parser_version="0.1",
            status="parsed",
        )
        s.add(stmt); s.flush()
        ids = []
        for i in range(3):
            tx = ExpenseTransaction(
                user_id="ariel", statement_id=stmt.id, source_id=src.id,
                occurred_on=date(2026, 5, 1 + i),
                merchant_raw="X", merchant_normalized="X",
                amount_nis=Decimal("10.00"), direction="debit",
                tx_type="regular", raw_row_json="{}",
                category_id=uncat.id, category_source="llm",
                category_confidence=Decimal("0.40"),
            )
            s.add(tx); s.flush(); ids.append(tx.id)
        s.commit()
        yield expense_client, ids


def test_patch_default_fans_out_for_backcompat(seeded_client):
    client, ids = seeded_client
    resp = client.patch(
        f"/api/expenses/transactions/{ids[0]}",
        json={"user_id": "ariel", "category_slug": "food.groceries"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["affected_count"] == 3

    # All three rows are now food.groceries, source=user.
    from argosy.state.models import ExpenseCategory, ExpenseTransaction
    SessionLocal = client.app.state.session_factory
    with SessionLocal() as s:
        food = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="food.groceries"
        ).one()
        for tx_id in ids:
            tx = s.get(ExpenseTransaction, tx_id)
            assert tx.category_id == food.id
            assert tx.category_source == "user"


def test_patch_with_apply_to_siblings_false_only_updates_one(seeded_client):
    client, ids = seeded_client
    resp = client.patch(
        f"/api/expenses/transactions/{ids[0]}",
        json={"user_id": "ariel", "category_slug": "food.groceries",
              "apply_to_siblings": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["affected_count"] == 1

    from argosy.state.models import (
        ExpenseCategory, ExpenseTransaction, MerchantCategoryCache,
    )
    SessionLocal = client.app.state.session_factory
    with SessionLocal() as s:
        food = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="food.groceries"
        ).one()
        tx0 = s.get(ExpenseTransaction, ids[0])
        assert tx0.category_id == food.id
        # Siblings unchanged
        for tx_id in ids[1:]:
            tx = s.get(ExpenseTransaction, tx_id)
            assert tx.category_id != food.id
        # No cache row was written.
        cache = s.query(MerchantCategoryCache).filter_by(
            user_id="ariel", merchant_pattern="X", is_regex=False,
        ).one_or_none()
        assert cache is None
