# tests/test_apply_merchant_category_service.py
"""Tests for argosy.services.merchant_service.apply_merchant_category."""
from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.merchant_service import (
    ApplyResult, apply_merchant_category, MerchantNotFoundError,
)
from argosy.state.models import (
    Base, ExpenseCategory, ExpenseTransaction, MerchantCategoryCache,
)


@pytest.fixture()
def session_factory(tmp_path):
    """Fresh file-backed SQLite session factory with schema created."""
    from argosy.services.expense_ingest.taxonomy_seed import seed_system_defaults

    db_path = tmp_path / "merchant_service_test.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)

    # Seed system-default categories once (required by seed_user_categories).
    with SF() as s:
        seed_system_defaults(s)
        s.commit()

    yield SF

    engine.dispose()


@pytest.fixture()
def session_with_user_and_categories(session_factory):
    """A session seeded with user 'ariel', the default taxonomy, and a few
    txs from merchant 'שטראוס' currently category=uncategorized.
    """
    from argosy.state.models import User
    from argosy.services.expense_ingest.taxonomy_seed import seed_user_categories
    with session_factory() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
        seed_user_categories(s, "ariel")
        s.flush()
        uncat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="uncategorized"
        ).one()
        # Need a source + statement to satisfy NOT NULL FKs; create minimal ones.
        from argosy.state.models import ExpenseSource, ExpenseStatement, UserFile
        from datetime import date
        from decimal import Decimal as D
        uf = UserFile(
            user_id="ariel", sha256="a" * 64,
            original_name="test.pdf", sanitized_name="test.pdf",
            mime_type="application/pdf", kind="other",
            size_bytes=1, storage_path="/tmp/test.pdf", source="chat_attachment",
        )
        s.add(uf); s.flush()
        src = ExpenseSource(user_id="ariel", kind="card", issuer="isracard",
                            external_id="0235", display_name="Test")
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id="ariel", source_id=src.id, file_id=uf.id,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
            parsed_total_nis=D("150.00"), parser_name="test", parser_version="0.1",
            status="parsed",
        )
        s.add(stmt); s.flush()
        for i in range(3):
            s.add(ExpenseTransaction(
                user_id="ariel", statement_id=stmt.id, source_id=src.id,
                occurred_on=date(2026, 5, 1 + i),
                merchant_raw="שטראוס בע\"מ", merchant_normalized="שטראוס",
                amount_nis=Decimal("50.00"), direction="debit",
                tx_type="regular", raw_row_json="{}",
                category_id=uncat.id, category_source="llm",
                category_confidence=Decimal("0.40"),
            ))
        s.commit()
        yield s


def test_apply_new_merchant_creates_cache_and_fans_out(session_with_user_and_categories):
    s = session_with_user_and_categories
    result = apply_merchant_category(
        s, user_id="ariel", merchant_normalized="שטראוס",
        category_slug="food.groceries",
    )
    assert isinstance(result, ApplyResult)
    assert result.cache_row_created is True
    assert result.affected_transactions == 3
    assert result.resolved_category_slug == "food.groceries"

    cache = s.query(MerchantCategoryCache).filter_by(
        user_id="ariel", merchant_pattern="שטראוס", is_regex=False,
    ).one()
    assert cache.source == "user"
    assert cache.confidence == Decimal("1.00")

    cat = s.query(ExpenseCategory).filter_by(
        user_id="ariel", slug="food.groceries"
    ).one()
    txs = s.query(ExpenseTransaction).filter_by(
        user_id="ariel", merchant_normalized="שטראוס"
    ).all()
    for tx in txs:
        assert tx.category_id == cat.id
        assert tx.category_source == "user"
        assert tx.category_confidence == Decimal("1.00")


def test_apply_existing_cache_row_overwrites(session_with_user_and_categories):
    s = session_with_user_and_categories
    apply_merchant_category(s, user_id="ariel",
                            merchant_normalized="שטראוס",
                            category_slug="food.groceries")
    s.commit()
    result = apply_merchant_category(
        s, user_id="ariel", merchant_normalized="שטראוס",
        category_slug="discretionary.shopping_other",
    )
    assert result.cache_row_created is False
    cache = s.query(MerchantCategoryCache).filter_by(
        user_id="ariel", merchant_pattern="שטראוס"
    ).one()
    new_cat = s.query(ExpenseCategory).filter_by(
        user_id="ariel", slug="discretionary.shopping_other"
    ).one()
    assert cache.category_id == new_cat.id


def test_apply_confirm_only_uses_most_common_category(
    session_with_user_and_categories
):
    s = session_with_user_and_categories
    # Set 2 of the 3 txs to food.groceries directly (simulating LLM verdict)
    food = s.query(ExpenseCategory).filter_by(
        user_id="ariel", slug="food.groceries"
    ).one()
    txs = s.query(ExpenseTransaction).filter_by(
        user_id="ariel", merchant_normalized="שטראוס"
    ).order_by(ExpenseTransaction.occurred_on).all()
    txs[0].category_id = food.id
    txs[1].category_id = food.id
    s.commit()

    result = apply_merchant_category(
        s, user_id="ariel", merchant_normalized="שטראוס", confirm=True,
    )
    assert result.resolved_category_slug == "food.groceries"
    cache = s.query(MerchantCategoryCache).filter_by(
        user_id="ariel", merchant_pattern="שטראוס"
    ).one()
    assert cache.source == "user"
    assert cache.confidence == Decimal("1.00")


def test_apply_rejects_both_slug_and_confirm(session_with_user_and_categories):
    s = session_with_user_and_categories
    with pytest.raises(ValueError, match="not both"):
        apply_merchant_category(
            s, user_id="ariel", merchant_normalized="שטראוס",
            category_slug="food.groceries", confirm=True,
        )


def test_apply_unknown_merchant_raises(session_with_user_and_categories):
    s = session_with_user_and_categories
    with pytest.raises(MerchantNotFoundError):
        apply_merchant_category(
            s, user_id="ariel", merchant_normalized="ghost-merchant",
            category_slug="food.groceries",
        )
