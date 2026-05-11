# argosy/services/merchant_service.py
"""Merchant↔category mapping primitive.

Single source of truth for "the user has decided what category this merchant
belongs to". Writes/updates a merchant_category_cache row AND every
expense_transactions row for this user with that merchant_normalized.

Used by:
  - PATCH /api/expenses/transactions/{id} (when apply_to_siblings=True)
  - PATCH /api/expenses/merchants/{merchant_normalized}
  - POST /api/expenses/merchants/bulk-category
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.state.models import (
    ExpenseCategory, ExpenseTransaction, MerchantCategoryCache,
)


class MerchantNotFoundError(Exception):
    """Raised when a merchant has no transactions for the given user."""


class CategoryNotFoundError(Exception):
    """Raised when the requested category slug does not exist for the user."""


class NothingToConfirmError(Exception):
    """Raised when confirm=True but the merchant has no categorized txs and
    no cache row, so there is no 'current category' to lock in.
    """


@dataclass(frozen=True)
class ApplyResult:
    merchant_normalized: str
    resolved_category_slug: str
    affected_transactions: int
    cache_row_created: bool


def apply_merchant_category(
    session: Session,
    *,
    user_id: str,
    merchant_normalized: str,
    category_slug: str | None = None,
    confirm: bool = False,
    source: str = "user",
    confidence: Decimal = Decimal("1.00"),
) -> ApplyResult:
    """Set the category for a merchant; fan out to all sibling transactions.

    Modes:
      - category_slug=X            → write/update cache row, set all txs to X.
      - confirm=True, slug=None    → keep the current category, just flip
                                     cache row to source=user, confidence=1.00.

    Raises MerchantNotFoundError if the user has no transactions for the
    given merchant_normalized AND no cache row.

    Caller is responsible for committing the session.
    """
    if not confirm and category_slug is None:
        raise ValueError("Must provide either category_slug or confirm=True")

    txs = session.query(ExpenseTransaction).filter(
        ExpenseTransaction.user_id == user_id,
        ExpenseTransaction.merchant_normalized == merchant_normalized,
    ).all()
    cache = session.query(MerchantCategoryCache).filter_by(
        user_id=user_id, merchant_pattern=merchant_normalized, is_regex=False,
    ).one_or_none()
    if not txs and cache is None:
        raise MerchantNotFoundError(
            f"No transactions or cache row for merchant '{merchant_normalized}'"
        )

    # Resolve target category
    if confirm and category_slug is None:
        target_slug = _infer_current_category_slug(session, user_id, txs, cache)
        if target_slug is None:
            raise NothingToConfirmError(
                f"Merchant '{merchant_normalized}' has no current category to confirm"
            )
    else:
        target_slug = category_slug  # type: ignore[assignment]

    cat = session.query(ExpenseCategory).filter_by(
        user_id=user_id, slug=target_slug
    ).one_or_none()
    if cat is None:
        raise CategoryNotFoundError(
            f"Category '{target_slug}' does not exist for user '{user_id}'"
        )

    now = datetime.now(timezone.utc)
    cache_created = False
    if cache is None:
        cache = MerchantCategoryCache(
            user_id=user_id, merchant_pattern=merchant_normalized,
            is_regex=False, category_id=cat.id,
            source=source, confidence=confidence,
            hit_count=1, last_hit_at=now,
        )
        session.add(cache)
        cache_created = True
    else:
        cache.category_id = cat.id
        cache.source = source
        cache.confidence = confidence
        cache.hit_count += 1
        cache.last_hit_at = now

    for tx in txs:
        tx.category_id = cat.id
        tx.category_source = source
        tx.category_confidence = confidence

    return ApplyResult(
        merchant_normalized=merchant_normalized,
        resolved_category_slug=target_slug,
        affected_transactions=len(txs),
        cache_row_created=cache_created,
    )


def _infer_current_category_slug(
    session: Session,
    user_id: str,
    txs: list[ExpenseTransaction],
    cache: MerchantCategoryCache | None,
) -> str | None:
    """For confirm-only mode, pick the category to lock in.

    Precedence:
      1. Existing cache row's category.
      2. The most-common category among the merchant's transactions
         (ties broken by most-recent occurred_on).
    """
    if cache is not None and cache.category_id is not None:
        cat = session.get(ExpenseCategory, cache.category_id)
        return cat.slug if cat else None
    if not txs:
        return None
    counts: Counter[int] = Counter()
    most_recent: dict[int, date] = {}
    for tx in txs:
        if tx.category_id is None:
            continue
        counts[tx.category_id] += 1
        if (tx.category_id not in most_recent
                or tx.occurred_on > most_recent[tx.category_id]):
            most_recent[tx.category_id] = tx.occurred_on
    if not counts:
        return None
    max_count = max(counts.values())
    winners = [cid for cid, c in counts.items() if c == max_count]
    winners.sort(key=lambda cid: most_recent[cid], reverse=True)
    cat = session.get(ExpenseCategory, winners[0])
    return cat.slug if cat else None
