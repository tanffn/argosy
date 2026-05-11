"""GET /api/expenses/merchants — merchant-aggregated listing."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture()
def seeded(expense_client):
    """Three merchants in three states:
      A — cache row (source=user, confidence=1.00), 2 txs food.groceries
      B — cache row (source=llm, confidence=0.92), 1 tx dining_out.restaurants
      C — no cache row, 3 txs in uncategorized
    """
    from argosy.services.expense_ingest.taxonomy_seed import (
        seed_system_defaults,
        seed_user_categories,
    )
    from argosy.state.models import (
        ExpenseCategory, ExpenseSource, ExpenseStatement,
        ExpenseTransaction, MerchantCategoryCache, UserFile,
    )
    from datetime import datetime, timezone
    SessionLocal = expense_client.app.state.session_factory
    with SessionLocal() as s:
        seed_system_defaults(s)
        s.flush()
        seed_user_categories(s, "ariel")
        s.flush()
        food = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="food.groceries"
        ).one()
        dining = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="dining_out.restaurants"
        ).one()
        uncat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="uncategorized"
        ).one()
        uf = UserFile(
            user_id="ariel", sha256="a" * 64,
            original_name="test.pdf", sanitized_name="test.pdf",
            mime_type="application/pdf", kind="other",
            size_bytes=1, storage_path="/tmp/test.pdf",
            source="chat_attachment",
        )
        s.add(uf); s.flush()
        src = ExpenseSource(user_id="ariel", kind="card", issuer="isracard",
                            external_id="1234", display_name="T")
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id="ariel", source_id=src.id, file_id=uf.id,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("60.00"),
            parser_name="test", parser_version="0.1",
            status="parsed",
        )
        s.add(stmt); s.flush()

        def mk(merch, cat, n):
            for i in range(n):
                s.add(ExpenseTransaction(
                    user_id="ariel", statement_id=stmt.id, source_id=src.id,
                    occurred_on=date(2026, 5, 1 + i),
                    merchant_raw=merch, merchant_normalized=merch,
                    amount_nis=Decimal("10.00"), direction="debit",
                    tx_type="regular", raw_row_json="{}",
                    category_id=cat.id, category_source="user",
                    category_confidence=Decimal("1.00"),
                ))
        mk("A", food, 2)
        mk("B", dining, 1)
        mk("C", uncat, 3)
        now = datetime.now(timezone.utc)
        s.add(MerchantCategoryCache(
            user_id="ariel", merchant_pattern="A", is_regex=False,
            category_id=food.id, source="user",
            confidence=Decimal("1.00"), hit_count=2, last_hit_at=now,
        ))
        s.add(MerchantCategoryCache(
            user_id="ariel", merchant_pattern="B", is_regex=False,
            category_id=dining.id, source="llm",
            confidence=Decimal("0.92"), hit_count=1, last_hit_at=now,
        ))
        s.commit()
    return expense_client


def test_list_all_three_merchants(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    norms = {m["merchant_normalized"] for m in body["merchants"]}
    assert norms == {"A", "B", "C"}


def test_filter_uncategorized(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel&category=uncategorized")
    body = r.json()
    assert body["total"] == 1
    assert body["merchants"][0]["merchant_normalized"] == "C"
    assert body["merchants"][0]["is_cached"] is False


def test_filter_by_source_user(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel&source=user")
    body = r.json()
    assert {m["merchant_normalized"] for m in body["merchants"]} == {"A"}


def test_filter_min_confidence(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel&min_confidence=0.95")
    body = r.json()
    # Only 'A' (1.00) qualifies.
    assert {m["merchant_normalized"] for m in body["merchants"]} == {"A"}


def test_search_substring(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel&search=c")
    body = r.json()
    assert {m["merchant_normalized"] for m in body["merchants"]} == {"C"}


def test_sort_by_tx_count_desc(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel&sort=tx_count&order=desc")
    body = r.json()
    counts = [m["tx_count"] for m in body["merchants"]]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] == 3


def test_default_sort_needs_attention_uncategorized_first(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel")
    body = r.json()
    assert body["merchants"][0]["merchant_normalized"] == "C"  # uncategorized first


def test_category_label_and_parent_label_populated(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel&sort=merchant&order=asc")
    body = r.json()
    a = next(m for m in body["merchants"] if m["merchant_normalized"] == "A")
    assert a["category_label"] == "Groceries"
    assert a["parent_slug"] == "food"
    assert a["parent_label"] == "Food (groceries)"
