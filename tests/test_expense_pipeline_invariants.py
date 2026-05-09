"""Pipeline conservation invariants — pass even when LLM is mocked.

These verify the plumbing, not model judgment. They MUST pass on every
build; if one fails, a parser/correlator/refund-matcher is silently
dropping or duplicating rows.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.orm import Session
from sqlalchemy import text

from argosy.agents.household_categorizer_types import CategorizeResult
from argosy.services.expense_ingest.orchestrator import ingest_user_file
from argosy.state.models import (
    ExpenseCategory, ExpenseTransaction, User, UserFile,
)

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


def _ingest(s: Session, fname: str) -> None:
    """Ingest a fixture file. Reuses or creates the 'ariel' user."""
    import hashlib
    if s.get(User, "ariel") is None:
        s.add(User(id="ariel", plan="free")); s.flush()
    p = FIXTURES / fname
    sha = hashlib.sha256(str(p).encode()).hexdigest()
    f = UserFile(user_id="ariel", sha256=sha, original_name=fname,
                 sanitized_name=fname, mime_type="x", kind="other",
                 size_bytes=p.stat().st_size, storage_path=str(p),
                 source="chat_attachment")
    s.add(f); s.commit()
    ingest_user_file(s, "ariel", f.id); s.commit()


def test_total_spend_equals_raw_sum(alembic_engine_at_head):
    """SUM of amounts is preserved through categorization."""
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm:
        # Stub LLM: send everything to discretionary (a non-excluded slug)
        def fake(uid, rows):
            return [CategorizeResult(tx_id=r.tx_id,
                                      category_slug="discretionary.shopping_other",
                                      confidence=0.95, rationale="x")
                    for r in rows]
        mock_llm.side_effect = fake
        _ingest(s, "max_minimal.xlsx")
        raw_total = s.execute(text(
            "SELECT SUM(amount_nis) FROM expense_transactions "
            "WHERE direction = 'debit' AND is_card_payment = 0"
        )).scalar()
        cat_totals = s.execute(text(
            "SELECT SUM(amount_nis) FROM expense_transactions "
            "WHERE category_id IS NOT NULL AND direction = 'debit' "
            "AND is_card_payment = 0"
        )).scalar()
        assert abs(float(raw_total or 0) - float(cat_totals or 0)) < 0.01


def test_card_payment_dedup_holds(alembic_engine_at_head):
    """If correlation has marked a row is_card_payment, it must have a
    matched_statement_id pointing somewhere real.
    """
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm:
        mock_llm.return_value = []
        _ingest(s, "leumi_osh_minimal.xls")
        # No card statements ingested; nothing should be marked is_card_payment
        rows = s.query(ExpenseTransaction).filter(
            ExpenseTransaction.is_card_payment.is_(True),
        ).all()
        for r in rows:
            assert r.matched_statement_id is not None


def test_refund_inheritance_consistent(alembic_engine_at_head):
    """Any refund with refund_of_id set must have category_id == prior.category_id."""
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm:
        # Make the LLM categorize WIZZ AIR as travel.flights so the refund
        # has something to inherit
        def fake(uid, rows):
            return [
                CategorizeResult(
                    tx_id=r.tx_id,
                    category_slug=("travel.flights" if "wizz" in r.merchant_normalized
                                    else "discretionary.shopping_other"),
                    confidence=0.95, rationale="x",
                )
                for r in rows
            ]
        mock_llm.side_effect = fake
        # Hand-build a corpus with a debit + refund pair
        s.add(User(id="ariel", plan="free")); s.flush()
        from argosy.services.expense_ingest.taxonomy_seed import (
            seed_system_defaults, seed_user_categories,
        )
        seed_system_defaults(s); seed_user_categories(s, "ariel"); s.flush()
        cat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="travel.flights").one()
        from argosy.state.models import ExpenseSource, ExpenseStatement
        f = UserFile(user_id="ariel", sha256="x"*64, original_name="x",
                     sanitized_name="x", mime_type="x", kind="other",
                     size_bytes=1, storage_path="/tmp/x", source="chat_attachment")
        s.add(f); s.flush()
        src = ExpenseSource(user_id="ariel", kind="card", issuer="max",
                            external_id="6225", display_name="Max 6225")
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id="ariel", source_id=src.id, file_id=f.id,
            period_start=date(2026, 2, 1), period_end=date(2026, 4, 30),
            parsed_total_nis=Decimal("0"), parser_name="max",
            parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        debit = ExpenseTransaction(
            user_id="ariel", statement_id=stmt.id, source_id=src.id,
            occurred_on=date(2026, 2, 12), merchant_raw="WIZZ AIR",
            merchant_normalized="wizz air", amount_nis=Decimal("2097.83"),
            direction="debit", tx_type="regular",
            category_id=cat.id, category_source="user",
            category_confidence=Decimal("1.00"),
            raw_row_json="{}",
        )
        refund = ExpenseTransaction(
            user_id="ariel", statement_id=stmt.id, source_id=src.id,
            occurred_on=date(2026, 3, 21), merchant_raw="WIZZ AIR",
            merchant_normalized="wizz air", amount_nis=Decimal("2097.83"),
            direction="credit", tx_type="refund", raw_row_json="{}",
        )
        s.add_all([debit, refund]); s.commit()

        from argosy.services.expense_ingest.refund_matcher import (
            match_refunds_for_user,
        )
        match_refunds_for_user(s, "ariel"); s.commit()

        s.refresh(refund)
        assert refund.refund_of_id == debit.id
        assert refund.category_id == debit.category_id
