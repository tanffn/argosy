"""Durable merchant[+category] → tag rules ("brush paint").

Exact-match on ``merchant_normalized`` (and optional ``category_slug``).
Substring matching is deliberately forbidden — a Paz fuel rule must not
tag groceries like ``פזית מרקט``.
"""

from __future__ import annotations

import json
import logging
from typing import Iterable

from sqlalchemy.orm import Session

from argosy.state.models import ExpenseCategory, ExpenseTagRule, ExpenseTransaction

logger = logging.getLogger(__name__)


def parse_tags(raw: str | None) -> list[str]:
    """Decode the JSON tag list. Tolerant of bad/empty JSON."""
    if not raw:
        return []
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [str(t) for t in v if isinstance(t, str)]
    except (ValueError, TypeError):
        pass
    return []


def serialize_tags(tags: Iterable[str]) -> str:
    """JSON-encode tags, deduped + sorted. Empty → ``'[]'`` (never NULL)."""
    seen: list[str] = []
    for t in tags:
        s = str(t).strip()
        if s and s not in seen:
            seen.append(s)
    return json.dumps(sorted(seen), ensure_ascii=False)


def apply_tag_rules(
    session: Session,
    user_id: str,
    tx_ids: list[int] | None = None,
    *,
    rule_ids: list[int] | None = None,
) -> int:
    """Idempotently add rule tags onto matching transactions.

    Returns the number of transactions whose tag list changed.
    """
    rules_q = session.query(ExpenseTagRule).filter_by(user_id=user_id)
    if rule_ids is not None:
        if not rule_ids:
            return 0
        rules_q = rules_q.filter(ExpenseTagRule.id.in_(rule_ids))
    rules = list(rules_q.all())
    if not rules:
        return 0

    # Prefetch category id → slug for optional category filters.
    cat_rows = (
        session.query(ExpenseCategory.id, ExpenseCategory.slug)
        .filter_by(user_id=user_id)
        .all()
    )
    slug_by_id = {cid: slug for cid, slug in cat_rows}
    id_by_slug = {slug: cid for cid, slug in cat_rows}

    changed = 0
    for rule in rules:
        tag = (rule.tag or "").strip()
        merchant = (rule.match_merchant_normalized or "").strip()
        if not tag or not merchant:
            continue

        q = session.query(ExpenseTransaction).filter(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.merchant_normalized == merchant,
        )
        if rule.match_category_slug:
            cat_id = id_by_slug.get(rule.match_category_slug)
            if cat_id is None:
                continue
            q = q.filter(ExpenseTransaction.category_id == cat_id)
        if tx_ids is not None:
            if not tx_ids:
                continue
            q = q.filter(ExpenseTransaction.id.in_(tx_ids))

        for tx in q.all():
            # Defense in depth: if category filter present, re-check slug
            # (category_id may have been reassigned since the join).
            if rule.match_category_slug:
                if slug_by_id.get(tx.category_id) != rule.match_category_slug:
                    continue
            cur = parse_tags(tx.tags)
            if tag in cur:
                continue
            cur.append(tag)
            tx.tags = serialize_tags(cur)
            changed += 1

    if changed:
        session.flush()
    return changed


def create_tag_rule(
    session: Session,
    user_id: str,
    *,
    match_merchant_normalized: str,
    tag: str,
    match_category_slug: str | None = None,
    apply_retroactive: bool = True,
) -> tuple[ExpenseTagRule, int]:
    """Insert a rule (idempotent on unique key) and optionally apply it.

    Returns ``(rule, tagged_count)``.
    """
    merchant = (match_merchant_normalized or "").strip()
    tag_clean = (tag or "").strip()
    if not merchant or not tag_clean:
        raise ValueError("match_merchant_normalized and tag are required")
    cat = (match_category_slug or "").strip() or None

    existing = (
        session.query(ExpenseTagRule)
        .filter_by(
            user_id=user_id,
            match_merchant_normalized=merchant,
            match_category_slug=cat,
            tag=tag_clean,
        )
        .one_or_none()
    )
    if existing is None:
        existing = ExpenseTagRule(
            user_id=user_id,
            match_merchant_normalized=merchant,
            match_category_slug=cat,
            tag=tag_clean,
        )
        session.add(existing)
        session.flush()

    tagged = 0
    if apply_retroactive:
        tagged = apply_tag_rules(
            session, user_id, rule_ids=[existing.id],
        )
    return existing, tagged


def bulk_add_tag(
    session: Session,
    user_id: str,
    tag: str,
    *,
    transaction_ids: list[int] | None = None,
    merchant_normalized: str | None = None,
    category_slug: str | None = None,
) -> int:
    """Add ``tag`` to txs selected by ids OR by merchant/category filter.

    Exactly one selection mode required. Returns count of txs whose tags
    changed.
    """
    tag_clean = (tag or "").strip()
    if not tag_clean:
        raise ValueError("tag is required")

    has_ids = bool(transaction_ids)
    has_filter = bool(
        (merchant_normalized and merchant_normalized.strip())
        or (category_slug and category_slug.strip())
    )
    if has_ids == has_filter:
        raise ValueError(
            "Provide either transaction_ids OR a merchant/category filter "
            "(not both, not neither)"
        )

    q = session.query(ExpenseTransaction).filter(
        ExpenseTransaction.user_id == user_id,
    )
    if has_ids:
        q = q.filter(ExpenseTransaction.id.in_(list(transaction_ids or [])))
    else:
        mn = (merchant_normalized or "").strip()
        if mn:
            q = q.filter(ExpenseTransaction.merchant_normalized == mn)
        cs = (category_slug or "").strip()
        if cs:
            cat = (
                session.query(ExpenseCategory)
                .filter_by(user_id=user_id, slug=cs)
                .one_or_none()
            )
            if cat is None:
                return 0
            q = q.filter(ExpenseTransaction.category_id == cat.id)

    changed = 0
    for tx in q.all():
        cur = parse_tags(tx.tags)
        if tag_clean in cur:
            continue
        cur.append(tag_clean)
        tx.tags = serialize_tags(cur)
        changed += 1
    if changed:
        session.flush()
    return changed


__all__ = [
    "parse_tags",
    "serialize_tags",
    "apply_tag_rules",
    "create_tag_rule",
    "bulk_add_tag",
]
