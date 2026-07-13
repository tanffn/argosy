"""Refund matcher: links direction='credit' tx_type='refund' rows to a
matching prior debit (same merchant_normalized, similar amount, within the
configured lookback) and inherits the prior's category. Runs AFTER
categorization.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.state.models import ExpenseTransaction

# Defaults — overridden by ExpensesRefundMatcherConfig when the caller
# passes lookback_days. 180 covers multi-month car-rental / hotel holds
# (e.g. vacation:romania Cars on Booking charge → refund ~114 days later).
LOOKBACK_DAYS = 180
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


def match_refunds_for_user(
    session: Session,
    user_id: str,
    *,
    lookback_days: int = LOOKBACK_DAYS,
) -> int:
    """Inherit category for unmatched refunds. Returns count newly matched.

    Same-day refunds are allowed (charge + cancel on the statement date).
    A debit already claimed by another refund is not reused.
    """
    if lookback_days <= 0:
        raise ValueError(f"lookback_days must be > 0; got {lookback_days!r}")

    refunds = session.query(ExpenseTransaction).filter(
        ExpenseTransaction.user_id == user_id,
        ExpenseTransaction.direction == "credit",
        ExpenseTransaction.tx_type == "refund",
        ExpenseTransaction.refund_of_id.is_(None),
    ).all()

    claimed_prior_ids = {
        int(pid) for (pid,) in session.query(ExpenseTransaction.refund_of_id).filter(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.refund_of_id.isnot(None),
        ).all()
        if pid is not None
    }

    matched = 0
    for refund in refunds:
        cutoff = refund.occurred_on - timedelta(days=lookback_days)
        # Pre-filter on date + merchant + categorized in SQL; do the
        # NULL-aware amount comparison in Python so foreign refunds
        # (amount_nis IS NULL) can still pair via amount_orig/currency_orig.
        # Same-day allowed: occurred_on <= refund.occurred_on, exclude self.
        candidates = session.query(ExpenseTransaction).filter(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.merchant_normalized == refund.merchant_normalized,
            ExpenseTransaction.direction == "debit",
            ExpenseTransaction.occurred_on >= cutoff,
            ExpenseTransaction.occurred_on <= refund.occurred_on,
            ExpenseTransaction.id != refund.id,
            ExpenseTransaction.category_id.isnot(None),
        ).order_by(
            ExpenseTransaction.occurred_on.desc(),
            ExpenseTransaction.id.desc(),
        ).all()
        prior = next(
            (
                c for c in candidates
                if c.id not in claimed_prior_ids and _amount_match(refund, c)
            ),
            None,
        )
        if prior is None:
            continue
        refund.refund_of_id = prior.id
        refund.category_id = prior.category_id
        refund.category_source = "inherited_from_refund"
        refund.category_confidence = prior.category_confidence
        claimed_prior_ids.add(prior.id)
        matched += 1
    session.flush()
    return matched
