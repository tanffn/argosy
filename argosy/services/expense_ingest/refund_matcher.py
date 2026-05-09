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


def _amount_match(refund: ExpenseTransaction, prior: ExpenseTransaction) -> bool:
    """True if refund and prior debit are amount-equivalent.

    Bug 2 (part 2): foreign rows carry amount_nis=None, so prefer NIS equality
    (with tolerance) when both sides have it; otherwise fall back to
    (amount_orig, currency_orig) equality (with tolerance).
    """
    if refund.amount_nis is not None and prior.amount_nis is not None:
        tol = refund.amount_nis * AMOUNT_TOLERANCE_PCT
        return (refund.amount_nis - tol) <= prior.amount_nis <= (refund.amount_nis + tol)
    if (
        refund.amount_orig is not None
        and prior.amount_orig is not None
        and refund.currency_orig is not None
        and refund.currency_orig == prior.currency_orig
    ):
        tol = refund.amount_orig * AMOUNT_TOLERANCE_PCT
        return (refund.amount_orig - tol) <= prior.amount_orig <= (refund.amount_orig + tol)
    return False


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
        # Pre-filter on date + merchant + categorized in SQL; do the
        # NULL-aware amount comparison in Python so foreign refunds
        # (amount_nis IS NULL) can still pair via amount_orig/currency_orig.
        candidates = session.query(ExpenseTransaction).filter(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.merchant_normalized == refund.merchant_normalized,
            ExpenseTransaction.direction == "debit",
            ExpenseTransaction.occurred_on >= cutoff,
            ExpenseTransaction.occurred_on < refund.occurred_on,
            ExpenseTransaction.category_id.isnot(None),
        ).order_by(ExpenseTransaction.occurred_on.desc()).all()
        prior = next((c for c in candidates if _amount_match(refund, c)), None)
        if prior is None:
            continue
        refund.refund_of_id = prior.id
        refund.category_id = prior.category_id
        refund.category_source = "inherited_from_refund"
        refund.category_confidence = prior.category_confidence
        matched += 1
    session.flush()
    return matched
