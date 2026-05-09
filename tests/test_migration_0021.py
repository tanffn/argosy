"""Schema assertions after migration 0021 (household expenses, 6 tables)."""

from sqlalchemy import inspect


def _columns(engine, table):
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table)}


def _indexes(engine, table):
    insp = inspect(engine)
    return {i["name"]: i for i in insp.get_indexes(table)}


def test_0021_creates_expense_sources(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "expense_sources")
    for name in ("id", "user_id", "kind", "issuer", "external_id",
                 "display_name", "cardholder_name", "active", "created_at"):
        assert name in cols, f"expense_sources missing column {name}"
    assert cols["user_id"]["nullable"] is False
    assert cols["cardholder_name"]["nullable"] is True


def test_0021_creates_expense_statements(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "expense_statements")
    for name in ("id", "user_id", "source_id", "file_id", "period_start",
                 "period_end", "charge_date", "declared_total_nis",
                 "parsed_total_nis", "parser_name", "parser_version",
                 "status", "parse_error", "ingested_at"):
        assert name in cols, f"expense_statements missing column {name}"


def test_0021_creates_expense_transactions(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "expense_transactions")
    for name in ("id", "user_id", "statement_id", "source_id",
                 "occurred_on", "posted_on", "merchant_raw",
                 "merchant_normalized", "amount_nis", "amount_orig",
                 "currency_orig", "direction", "tx_type", "reference",
                 "category_id", "category_source", "category_confidence",
                 "is_card_payment", "matched_statement_id", "refund_of_id",
                 "raw_row_json", "ingested_at"):
        assert name in cols, f"expense_transactions missing column {name}"


def test_0021_creates_expense_categories(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "expense_categories")
    for name in ("id", "user_id", "slug", "label_en", "label_he",
                 "parent_id", "is_excluded_from_spend", "is_inflow",
                 "display_order"):
        assert name in cols
    assert cols["user_id"]["nullable"] is True  # NULL = system-default rows


def test_0021_creates_merchant_category_cache(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "merchant_category_cache")
    for name in ("id", "user_id", "merchant_pattern", "is_regex",
                 "category_id", "source", "confidence", "hit_count",
                 "last_hit_at", "created_at"):
        assert name in cols


def test_0021_creates_expense_review_queue(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "expense_review_queue")
    for name in ("id", "user_id", "kind", "status", "payload_json",
                 "related_tx_id", "related_source_id", "user_note",
                 "created_at", "resolved_at"):
        assert name in cols


def test_0021_indexes_are_present(alembic_engine_at_head):
    tx_idx = _indexes(alembic_engine_at_head, "expense_transactions")
    have = set(tx_idx.keys())
    assert any("occurred_on" in n for n in have), \
        f"expected occurred_on index on expense_transactions; have {have}"
    assert any("merchant_normalized" in n for n in have)
    cache_idx = _indexes(alembic_engine_at_head, "merchant_category_cache")
    assert any("merchant_pattern" in n for n in cache_idx.keys())


def test_0021_orm_round_trip(alembic_engine_at_head):
    """Insert + read each new ORM class to confirm models match the schema."""
    from sqlalchemy.orm import Session
    from datetime import date

    from argosy.state.models import (
        ExpenseSource, ExpenseStatement, ExpenseTransaction,
        ExpenseCategory, MerchantCategoryCache, ExpenseReviewQueue,
        User, UserFile,
    )

    with Session(alembic_engine_at_head) as s:
        s.add(User(id="ariel", plan="free"))
        s.flush()
        s.add(UserFile(
            user_id="ariel", sha256="a" * 64,
            original_name="x.xls", sanitized_name="x.xls",
            mime_type="application/vnd.ms-excel", kind="other",
            size_bytes=1, storage_path="/tmp/x.xls", source="chat_attachment",
        ))
        s.flush()
        cat = ExpenseCategory(slug="food.groceries", label_en="Groceries",
                              label_he="מצרכי מזון")
        s.add(cat)
        s.flush()
        src = ExpenseSource(user_id="ariel", kind="card", issuer="isracard",
                            external_id="1266", display_name="Isracard 1266",
                            cardholder_name="ariel")
        s.add(src)
        s.flush()
        stmt = ExpenseStatement(
            user_id="ariel", source_id=src.id, file_id=1,
            period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
            charge_date=date(2026, 4, 15), parsed_total_nis=3319.44,
            parser_name="isracard", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt)
        s.flush()
        tx = ExpenseTransaction(
            user_id="ariel", statement_id=stmt.id, source_id=src.id,
            occurred_on=date(2026, 4, 8), merchant_raw="NETFLIX.COM",
            merchant_normalized="netflix.com", amount_nis=69.90,
            direction="debit", tx_type="standing_order", raw_row_json="{}",
        )
        s.add(tx)
        s.add(MerchantCategoryCache(
            user_id="ariel", merchant_pattern="netflix.com",
            category_id=cat.id, source="user", confidence=1.00,
        ))
        s.add(ExpenseReviewQueue(
            user_id="ariel", kind="uncategorized",
            payload_json='{"merchant_normalized": "x"}',
        ))
        s.commit()
        assert s.query(ExpenseTransaction).count() == 1
        assert s.query(ExpenseReviewQueue).filter_by(status="open").count() == 1
