"""Tests for the category cascade: user → issuer → cache → LLM → uncategorized."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy.orm import Session

from argosy.agents.household_categorizer_types import CategorizeResult
from argosy.services.expense_ingest.category_resolver import (
    resolve_categories_for_user,
)
from argosy.services.expense_ingest.taxonomy_seed import (
    seed_system_defaults, seed_user_categories,
)
from argosy.state.models import (
    ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
    MerchantCategoryCache, User, UserFile,
)


def _seed_world(s: Session) -> dict:
    s.add(User(id="ariel", plan="free")); s.flush()
    seed_system_defaults(s)
    seed_user_categories(s, "ariel")
    s.flush()
    f = UserFile(user_id="ariel", sha256="a"*64, original_name="x",
                 sanitized_name="x", mime_type="x", kind="other",
                 size_bytes=1, storage_path="/tmp/x", source="chat_attachment")
    s.add(f); s.flush()
    src = ExpenseSource(user_id="ariel", kind="card", issuer="max",
                        external_id="6225", display_name="Max 6225")
    s.add(src); s.flush()
    stmt = ExpenseStatement(
        user_id="ariel", source_id=src.id, file_id=f.id,
        period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
        parsed_total_nis=Decimal("0"), parser_name="max",
        parser_version="0.1.0", status="parsed",
    )
    s.add(stmt); s.flush()
    return {"src": src, "stmt": stmt}


def _add_tx(s: Session, ctx: dict, *, merchant: str, anaf: str | None = None,
            direction: str = "debit", tx_type: str = "regular") -> ExpenseTransaction:
    tx = ExpenseTransaction(
        user_id="ariel", statement_id=ctx["stmt"].id, source_id=ctx["src"].id,
        occurred_on=date(2026, 4, 10), merchant_raw=merchant,
        merchant_normalized=merchant.lower(),
        amount_nis=Decimal("100"), direction=direction, tx_type=tx_type,
        raw_row_json="{}",
    )
    if anaf:
        tx.raw_row_json = f'{{"anaf": "{anaf}"}}'
    s.add(tx); s.flush()
    return tx


def test_user_override_wins(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_world(s)
        cat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="food.groceries").one()
        s.add(MerchantCategoryCache(
            user_id="ariel", merchant_pattern="netflix.com",
            category_id=cat.id, source="user", confidence=Decimal("1.00"),
        ))
        s.commit()
        tx = _add_tx(s, ctx, merchant="NETFLIX.COM")
        s.commit()
        with patch("argosy.services.expense_ingest.category_resolver"
                   "._categorize_via_llm") as mock_llm:
            n = resolve_categories_for_user(s, "ariel")
            s.commit()
            mock_llm.assert_not_called()  # cache hit short-circuits LLM
        s.refresh(tx)
        assert tx.category_id == cat.id
        assert tx.category_source == "cache"  # via cache, not 'user' direct


def test_issuer_seed_wins_when_unambiguous(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_world(s); s.commit()
        tx = _add_tx(s, ctx, merchant="ספייס אינביידרז", anaf="מסעדות")
        s.commit()
        with patch("argosy.services.expense_ingest.category_resolver"
                   "._categorize_via_llm") as mock_llm:
            n = resolve_categories_for_user(s, "ariel")
            s.commit()
            mock_llm.assert_not_called()
        s.refresh(tx)
        cat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="dining_out.restaurants").one()
        assert tx.category_id == cat.id
        assert tx.category_source == "issuer"


def test_llm_called_for_unknown(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_world(s); s.commit()
        tx = _add_tx(s, ctx, merchant="UNKNOWN VENDOR XYZ")
        s.commit()
        with patch("argosy.services.expense_ingest.category_resolver"
                   "._categorize_via_llm") as mock_llm:
            mock_llm.return_value = [CategorizeResult(
                tx_id=tx.id, category_slug="dining_out.restaurants",
                confidence=0.95, rationale="Looks like a restaurant",
            )]
            resolve_categories_for_user(s, "ariel")
            s.commit()
            assert mock_llm.call_count == 1
        s.refresh(tx)
        cat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="dining_out.restaurants").one()
        assert tx.category_id == cat.id
        assert tx.category_source == "llm"
        # Cache row must have been written
        cache = s.query(MerchantCategoryCache).filter_by(
            merchant_pattern="unknown vendor xyz").one()
        assert cache.source == "llm"


def test_uncategorized_below_threshold(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_world(s); s.commit()
        tx = _add_tx(s, ctx, merchant="WEIRD MERCHANT")
        s.commit()
        with patch("argosy.services.expense_ingest.category_resolver"
                   "._categorize_via_llm") as mock_llm:
            mock_llm.return_value = [CategorizeResult(
                tx_id=tx.id, category_slug="uncategorized",
                confidence=0.30, rationale="ambiguous",
            )]
            resolve_categories_for_user(s, "ariel")
            s.commit()
        s.refresh(tx)
        unc = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="uncategorized").one()
        assert tx.category_id == unc.id
        assert tx.category_source == "llm"


def test_refunds_not_sent_to_resolver(alembic_engine_at_head):
    """Refunds must be filtered out before the resolver runs."""
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_world(s); s.commit()
        refund = _add_tx(s, ctx, merchant="WIZZ AIR",
                          direction="credit", tx_type="refund")
        s.commit()
        with patch("argosy.services.expense_ingest.category_resolver"
                   "._categorize_via_llm") as mock_llm:
            resolve_categories_for_user(s, "ariel")
            s.commit()
            mock_llm.assert_not_called()
        s.refresh(refund)
        assert refund.category_id is None
