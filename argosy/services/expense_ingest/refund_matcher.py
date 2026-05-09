"""Refund matcher: links direction='credit' tx_type='refund' rows to a
matching prior debit (same merchant_normalized, similar amount, within 90
days prior) and inherits the prior's category. Runs AFTER categorization.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.state.models import ExpenseTransaction

LOOKBACK_DAYS = 90
AMOUNT_TOLERANCE_PCT = Decimal("0.05")


def match_refunds_for_user(session: Session, user_id: str) -> int:
    """Inherit category for unmatched refunds. Returns count newly matched."""
    refunds = session.query(ExpenseTransaction).filter(
        ExpenseTransaction.user_id == user_id,
        ExpenseTransaction.direction == "credit",
        ExpenseTransaction.tx_type == "refund",
        ExpenseTransaction.refund_of_id.is_(None),
    ).all()

    matched = 0
    for refund in refunds:
        cutoff = refund.occurred_on - timedelta(days=LOOKBACK_DAYS)
        tolerance = refund.amount_nis * AMOUNT_TOLERANCE_PCT
        candidates = session.query(ExpenseTransaction).filter(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.merchant_normalized == refund.merchant_normalized,
            ExpenseTransaction.direction == "debit",
            ExpenseTransaction.occurred_on >= cutoff,
            ExpenseTransaction.occurred_on < refund.occurred_on,
            ExpenseTransaction.amount_nis >= refund.amount_nis - tolerance,
            ExpenseTransaction.amount_nis <= refund.amount_nis + tolerance,
            ExpenseTransaction.category_id.isnot(None),
        ).order_by(ExpenseTransaction.occurred_on.desc()).all()
        if not candidates:
            continue
        prior = candidates[0]
        refund.refund_of_id = prior.id
        refund.category_id = prior.category_id
        refund.category_source = "inherited_from_refund"
        refund.category_confidence = prior.category_confidence
        matched += 1
    session.flush()
    return matched
