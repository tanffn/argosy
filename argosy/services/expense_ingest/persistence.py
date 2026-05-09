"""Persistence helpers — idempotent inserts for statements + transactions.

Statement uniqueness: (user_id, source_id, period_start, period_end).
Transaction content-hash key: (statement_id, occurred_on, merchant_raw,
amount_nis, reference). Re-running on the same parsed file produces zero
new transaction rows.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.types import (
    NormalizedTransaction, ParseResult, ParserName,
)
from argosy.state.models import ExpenseStatement, ExpenseTransaction


def _content_key(statement_id: int, tx: NormalizedTransaction) -> str:
    parts = [
        str(statement_id),
        tx.occurred_on.isoformat(),
        tx.merchant_raw,
        f"{tx.amount_nis:.2f}",
        tx.reference or "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def persist_statement(
    session: Session,
    user_id: str,
    source_id: int,
    file_id: int,
    result: ParseResult,
    parser: ParserName,
    parser_version: str,
) -> ExpenseStatement:
    """Find or insert the ExpenseStatement row for this parse result."""
    existing = session.query(ExpenseStatement).filter_by(
        user_id=user_id, source_id=source_id,
        period_start=result.statement.period_start,
        period_end=result.statement.period_end,
    ).one_or_none()
    if existing is not None:
        return existing

    stmt = ExpenseStatement(
        user_id=user_id, source_id=source_id, file_id=file_id,
        period_start=result.statement.period_start,
        period_end=result.statement.period_end,
        charge_date=result.statement.charge_date,
        declared_total_nis=Decimal(str(result.statement.declared_total_nis))
            if result.statement.declared_total_nis is not None else None,
        parsed_total_nis=Decimal(str(result.statement.parsed_total_nis)),
        parser_name=parser.value,
        parser_version=parser_version,
        status="parsed",
    )
    session.add(stmt)
    session.flush()
    return stmt


def persist_transactions(
    session: Session,
    stmt: ExpenseStatement,
    source_id: int,
    user_id: str,
    txs: list[NormalizedTransaction],
) -> int:
    """Insert transactions for a statement; skip rows whose content hash
    already exists. Returns the count of newly-inserted rows.
    """
    inserted = 0
    seen_keys = set()
    for tx in txs:
        key = _content_key(stmt.id, tx)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        existing = session.query(ExpenseTransaction).filter_by(
            statement_id=stmt.id, occurred_on=tx.occurred_on,
            merchant_raw=tx.merchant_raw,
        ).filter(
            ExpenseTransaction.amount_nis == Decimal(str(tx.amount_nis)),
            ExpenseTransaction.reference == tx.reference,
        ).first()
        if existing is not None:
            continue
        row = ExpenseTransaction(
            user_id=user_id, statement_id=stmt.id, source_id=source_id,
            occurred_on=tx.occurred_on, posted_on=tx.posted_on,
            merchant_raw=tx.merchant_raw,
            merchant_normalized=tx.merchant_normalized,
            amount_nis=Decimal(str(tx.amount_nis)),
            amount_orig=Decimal(str(tx.amount_orig))
                if tx.amount_orig is not None else None,
            currency_orig=tx.currency_orig,
            direction=tx.direction,
            tx_type=tx.tx_type,
            reference=tx.reference,
            category_id=None,
            category_source=None,
            category_confidence=None,
            is_card_payment=False,
            matched_statement_id=None,
            refund_of_id=None,
            raw_row_json=json.dumps(tx.raw_row, ensure_ascii=False, default=str),
        )
        session.add(row)
        inserted += 1
    session.flush()
    return inserted
