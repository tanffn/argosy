"""Tests for statement + transaction persistence with content-hash dedup."""

from datetime import date

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.persistence import (
    persist_statement, persist_transactions,
)
from argosy.services.expense_ingest.types import (
    NormalizedTransaction, ParseResult, ParserName, SourceHint, StatementMeta,
)
from argosy.services.expense_ingest.registry import register_or_get_source
from argosy.state.models import (
    ExpenseStatement, ExpenseTransaction, User, UserFile,
)


def _seed(s: Session) -> int:
    s.add(User(id="ariel", plan="free"))
    s.flush()
    f = UserFile(
        user_id="ariel", sha256="a" * 64, original_name="x.xlsx",
        sanitized_name="x.xlsx", mime_type="application/vnd...sheet",
        kind="other", size_bytes=1, storage_path="/tmp/x", source="chat_attachment",
    )
    s.add(f)
    s.flush()
    return f.id


def _result() -> ParseResult:
    txs = [NormalizedTransaction(
        occurred_on=date(2026, 4, 8), merchant_raw="A",
        merchant_normalized="a", amount_nis=10, direction="debit",
        tx_type="regular",
    )]
    return ParseResult(
        statement=StatementMeta(
            period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
            charge_date=date(2026, 4, 15),
            declared_total_nis=10, parsed_total_nis=10,
        ),
        transactions=txs,
    )


def test_persist_statement_creates_row(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        file_id = _seed(s)
        src = register_or_get_source(s, "ariel", SourceHint(
            kind="card", issuer="isracard", external_id="1266"))
        s.commit()
        stmt = persist_statement(s, "ariel", src.id, file_id, _result(),
                                 ParserName.ISRACARD, "0.1.0")
        s.commit()
        assert stmt.id is not None
        assert stmt.status == "parsed"


def test_persist_statement_is_idempotent(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        file_id = _seed(s)
        src = register_or_get_source(s, "ariel", SourceHint(
            kind="card", issuer="isracard", external_id="1266"))
        s.commit()
        stmt1 = persist_statement(s, "ariel", src.id, file_id, _result(),
                                  ParserName.ISRACARD, "0.1.0")
        s.commit()
        stmt2 = persist_statement(s, "ariel", src.id, file_id, _result(),
                                  ParserName.ISRACARD, "0.1.0")
        s.commit()
        assert stmt1.id == stmt2.id
        assert s.query(ExpenseStatement).count() == 1


def test_persist_transactions_dedupes_by_content_hash(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        file_id = _seed(s)
        src = register_or_get_source(s, "ariel", SourceHint(
            kind="card", issuer="isracard", external_id="1266"))
        s.commit()
        result = _result()
        stmt = persist_statement(s, "ariel", src.id, file_id, result,
                                 ParserName.ISRACARD, "0.1.0")
        s.commit()
        n1 = persist_transactions(s, stmt, src.id, "ariel", result.transactions)
        s.commit()
        n2 = persist_transactions(s, stmt, src.id, "ariel", result.transactions)
        s.commit()
        assert n1 == 1
        assert n2 == 0
        assert s.query(ExpenseTransaction).count() == 1
