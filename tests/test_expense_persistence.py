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


def _rolling_result(txs) -> ParseResult:
    return ParseResult(
        statement=StatementMeta(
            period_start=min(t.occurred_on for t in txs),
            period_end=max(t.occurred_on for t in txs),
            charge_date=None, declared_total_nis=None,
            parsed_total_nis=sum(t.amount_nis for t in txs),
        ),
        transactions=txs,
        rolling=True,
    )


def test_rolling_export_dedupes_source_wide(alembic_engine_at_head):
    """Max rolling 90-day exports overlap prior MONTHLY statements (owner
    case 2026-07-12: 6225_2026_Jul_12.xlsx, window Apr-18..Jul-09 over
    already-ingested May/June). Source-scoped dedup must skip the overlap
    rows even though (a) they live in a DIFFERENT statement and (b) the
    monthly rows may carry a reference while rolling rows never do."""
    with Session(alembic_engine_at_head) as s:
        file_id = _seed(s)
        src = register_or_get_source(s, "ariel", SourceHint(
            kind="card", issuer="max", external_id="6225"))
        s.commit()

        # Monthly statement with one row (carrying a reference).
        monthly_tx = NormalizedTransaction(
            occurred_on=date(2026, 5, 10), merchant_raw="SUPER",
            merchant_normalized="super", amount_nis=120, direction="debit",
            tx_type="regular", reference="V123",
        )
        monthly = ParseResult(
            statement=StatementMeta(
                period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
                charge_date=date(2026, 6, 2), declared_total_nis=120,
                parsed_total_nis=120,
            ),
            transactions=[monthly_tx],
        )
        stmt_m = persist_statement(s, "ariel", src.id, file_id, monthly,
                                   ParserName.MAX, "0.1.0")
        s.commit()
        assert persist_transactions(
            s, stmt_m, src.id, "ariel", monthly.transactions) == 1
        s.commit()

        # Rolling export: the SAME purchase (no reference) + one new one.
        overlap = NormalizedTransaction(
            occurred_on=date(2026, 5, 10), merchant_raw="SUPER",
            merchant_normalized="super", amount_nis=120, direction="debit",
            tx_type="regular", reference=None,
        )
        fresh = NormalizedTransaction(
            occurred_on=date(2026, 7, 9), merchant_raw="TERMINAL X",
            merchant_normalized="terminal x", amount_nis=193.83,
            direction="debit", tx_type="regular", reference=None,
        )
        rolling = _rolling_result([overlap, fresh])
        stmt_r = persist_statement(s, "ariel", src.id, file_id, rolling,
                                   ParserName.MAX, "0.1.0")
        s.commit()
        inserted = persist_transactions(
            s, stmt_r, src.id, "ariel", rolling.transactions,
            dedup_scope="source")
        s.commit()
        assert inserted == 1, "overlap row must be skipped, fresh row kept"
        assert s.query(ExpenseTransaction).count() == 2

        # Re-running the same rolling file inserts nothing.
        assert persist_transactions(
            s, stmt_r, src.id, "ariel", rolling.transactions,
            dedup_scope="source") == 0
