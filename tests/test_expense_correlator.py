"""Tests for the bank ↔ card-statement correlator."""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.correlator import correlate_for_user
from argosy.state.models import (
    ExpenseSource, ExpenseStatement, ExpenseTransaction, User, UserFile,
)


def _seed_minimal(s: Session) -> dict:
    s.add(User(id="ariel", plan="free"))
    s.flush()
    f = UserFile(
        user_id="ariel", sha256="a" * 64, original_name="x",
        sanitized_name="x", mime_type="application/octet-stream",
        kind="other", size_bytes=1, storage_path="/tmp/x",
        source="chat_attachment",
    )
    s.add(f); s.flush()
    bank = ExpenseSource(user_id="ariel", kind="bank", issuer="leumi",
                         external_id="44745280", display_name="Leumi 44745280")
    card = ExpenseSource(user_id="ariel", kind="card", issuer="isracard",
                         external_id="1266", display_name="Isracard 1266")
    s.add_all([bank, card]); s.flush()
    bank_stmt = ExpenseStatement(
        user_id="ariel", source_id=bank.id, file_id=f.id,
        period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
        parsed_total_nis=Decimal("0"), parser_name="leumi_osh",
        parser_version="0.1.0", status="parsed",
    )
    card_stmt = ExpenseStatement(
        user_id="ariel", source_id=card.id, file_id=f.id,
        period_start=date(2026, 3, 16), period_end=date(2026, 4, 15),
        charge_date=date(2026, 4, 15),
        declared_total_nis=Decimal("3319.44"),
        parsed_total_nis=Decimal("3319.44"),
        parser_name="isracard", parser_version="0.1.0", status="parsed",
    )
    s.add_all([bank_stmt, card_stmt]); s.flush()
    bank_tx = ExpenseTransaction(
        user_id="ariel", statement_id=bank_stmt.id, source_id=bank.id,
        occurred_on=date(2026, 4, 15), merchant_raw="ל.מאסטרקרד(יש)",
        merchant_normalized="ל.מאסטרקרד(יש)",
        amount_nis=Decimal("3319.44"), direction="debit", tx_type="regular",
        reference="1266", raw_row_json="{}",
    )
    s.add(bank_tx); s.flush()
    return {"bank": bank, "card": card, "bank_stmt": bank_stmt,
            "card_stmt": card_stmt, "bank_tx": bank_tx}


def test_correlator_links_via_reference_and_amount(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_minimal(s)
        s.commit()
        n = correlate_for_user(s, "ariel")
        s.commit()
        assert n == 1
        s.refresh(ctx["bank_tx"])
        assert ctx["bank_tx"].is_card_payment is True
        assert ctx["bank_tx"].matched_statement_id == ctx["card_stmt"].id


def test_correlator_skips_unknown_reference(alembic_engine_at_head):
    """Numeric ref that doesn't match an expense_sources.external_id stays uncorrelated."""
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_minimal(s)
        ctx["bank_tx"].reference = "99999"
        s.commit()
        n = correlate_for_user(s, "ariel")
        s.commit()
        s.refresh(ctx["bank_tx"])
        assert n == 0
        assert ctx["bank_tx"].is_card_payment is False


def test_correlator_amount_fallback(alembic_engine_at_head):
    """When ref is empty but amount + date match a card statement, link."""
    with Session(alembic_engine_at_head) as s:
        ctx = _seed_minimal(s)
        ctx["bank_tx"].reference = None
        s.commit()
        n = correlate_for_user(s, "ariel")
        s.commit()
        s.refresh(ctx["bank_tx"])
        assert n == 1
        assert ctx["bank_tx"].matched_statement_id == ctx["card_stmt"].id


def test_correlator_is_idempotent(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed_minimal(s); s.commit()
        n1 = correlate_for_user(s, "ariel"); s.commit()
        n2 = correlate_for_user(s, "ariel"); s.commit()
        assert n1 == 1
        assert n2 == 0
