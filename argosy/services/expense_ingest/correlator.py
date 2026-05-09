"""Bank ↔ card-statement correlator. Marks bank rows that pay a card
statement total so they don't double-count itemized card spend.

Tier 1: bank_tx.reference matches an existing ExpenseSource.external_id
(card kind), amount within tolerance, date within window.

Tier 2: bank_tx.reference is None or unknown — fall back to amount + date
exact match against a single card statement.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.state.models import (
    ExpenseSource, ExpenseStatement, ExpenseTransaction,
)

AMOUNT_TOLERANCE_NIS = Decimal("50")
DATE_WINDOW_DAYS = 2


def _smells_like_card_payment(merchant: str) -> bool:
    keywords = ("ל.מאסטרקרד", "כרטיסי אשראי", "ויזה", "דיינרס",
                "אמריקן אקספרס", "ישראכרט", "מאסטרקרד")
    return any(k in merchant for k in keywords)


def correlate_for_user(session: Session, user_id: str) -> int:
    """Run correlation across all unmatched bank-side rows for this user.
    Returns the number of new matches made.
    """
    candidates = (
        session.query(ExpenseTransaction)
        .join(ExpenseSource, ExpenseSource.id == ExpenseTransaction.source_id)
        .filter(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.is_card_payment.is_(False),
            ExpenseSource.kind == "bank",
        )
        .all()
    )

    card_sources = session.query(ExpenseSource).filter_by(
        user_id=user_id, kind="card",
    ).all()
    by_external = {src.external_id: src for src in card_sources}

    matches = 0
    for tx in candidates:
        if not _smells_like_card_payment(tx.merchant_raw):
            continue

        stmt: ExpenseStatement | None = None
        if tx.reference and tx.reference in by_external:
            src = by_external[tx.reference]
            stmt = _find_card_statement(session, src.id, tx.occurred_on,
                                        tx.amount_nis)
        if stmt is None and tx.reference is None:
            stmt = _find_by_amount_date(session, user_id, tx.amount_nis,
                                        tx.occurred_on)

        if stmt is not None:
            tx.is_card_payment = True
            tx.matched_statement_id = stmt.id
            matches += 1

    session.flush()
    return matches


def _find_card_statement(
    session: Session, source_id: int, target_date, amount: Decimal,
) -> ExpenseStatement | None:
    candidates = session.query(ExpenseStatement).filter(
        ExpenseStatement.source_id == source_id,
        ExpenseStatement.charge_date.isnot(None),
    ).all()
    for stmt in candidates:
        if abs((stmt.charge_date - target_date).days) > DATE_WINDOW_DAYS:
            continue
        if stmt.declared_total_nis is None:
            continue
        if abs(stmt.declared_total_nis - amount) <= AMOUNT_TOLERANCE_NIS:
            return stmt
    return None


def _find_by_amount_date(
    session: Session, user_id: str, amount: Decimal, target_date,
) -> ExpenseStatement | None:
    candidates = session.query(ExpenseStatement).join(
        ExpenseSource, ExpenseSource.id == ExpenseStatement.source_id,
    ).filter(
        ExpenseStatement.user_id == user_id,
        ExpenseSource.kind == "card",
        ExpenseStatement.charge_date == target_date,
    ).all()
    matching = [
        s for s in candidates
        if s.declared_total_nis is not None
        and abs(s.declared_total_nis - amount) < Decimal("0.50")
    ]
    if len(matching) == 1:
        return matching[0]
    return None
