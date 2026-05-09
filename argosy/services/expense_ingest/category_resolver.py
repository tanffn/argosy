"""Category resolver — the cascade.

Order, per spec §7.1 (non-refund rows only):
  1. user-override cache hit  → category, source='cache' (cached value)
  2. issuer-seeded category    → unambiguous → use slug, source='issuer'
                                  ambiguous   → drop hint and fall through
  3. LLM cache hit              → reuse cached LLM verdict
  4. LLM batch call             → write cache, ≥0.85 → category;
                                  <0.85 → 'uncategorized', source='llm'

Refunds are filtered out BEFORE this stage; their category is set later by
``refund_matcher`` from the matched prior debit.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.agents.household_categorizer import HouseholdCategorizerAgent
from argosy.agents.household_categorizer_types import (
    CategorizeResult, CategorizeRow,
)
from argosy.services.expense_ingest.issuer_seed import map_issuer_category
from argosy.state.models import (
    ExpenseCategory, ExpenseSource, ExpenseTransaction, MerchantCategoryCache,
)


def resolve_categories_for_user(session: Session, user_id: str) -> int:
    """Resolve categories for all uncategorized non-refund transactions.

    Returns the count of rows newly categorized.
    """
    candidates = session.query(ExpenseTransaction).filter(
        ExpenseTransaction.user_id == user_id,
        ExpenseTransaction.category_id.is_(None),
        ~((ExpenseTransaction.direction == "credit") &
          (ExpenseTransaction.tx_type == "refund")),
    ).all()
    if not candidates:
        return 0

    cats_by_slug = {
        c.slug: c for c in session.query(ExpenseCategory).filter_by(
            user_id=user_id
        ).all()
    }
    uncat_id = cats_by_slug["uncategorized"].id
    cache_by_pattern = {
        r.merchant_pattern: r for r in session.query(MerchantCategoryCache)
        .filter_by(user_id=user_id, is_regex=False).all()
    }

    llm_batch: list[tuple[ExpenseTransaction, str | None]] = []
    resolved = 0

    for tx in candidates:
        # 1. Cache hit
        cached = cache_by_pattern.get(tx.merchant_normalized)
        if cached is not None:
            tx.category_id = cached.category_id
            tx.category_source = "cache"
            tx.category_confidence = cached.confidence
            cached.hit_count += 1
            cached.last_hit_at = datetime.utcnow()
            resolved += 1
            continue

        # 2. Issuer seed
        anaf = _extract_anaf_from_raw_row(tx.raw_row_json)
        seed = map_issuer_category(anaf)
        if seed.slug is not None and not seed.defer_to_llm:
            cat = cats_by_slug.get(seed.slug)
            if cat is not None:
                tx.category_id = cat.id
                tx.category_source = "issuer"
                tx.category_confidence = Decimal(str(seed.confidence))
                resolved += 1
                continue

        # 3+4. Defer to LLM (with hint if ambiguous)
        llm_batch.append((tx, seed.hint))

    if llm_batch:
        rows = [
            CategorizeRow(
                tx_id=tx.id,
                merchant_normalized=tx.merchant_normalized,
                merchant_raw=tx.merchant_raw,
                amount_nis=float(tx.amount_nis),
                direction=tx.direction,
                occurred_on=tx.occurred_on,
                issuer_kind=session.get(ExpenseSource, tx.source_id).kind,
                issuer_name=session.get(ExpenseSource, tx.source_id).issuer,
                issuer_category_he=hint,
            )
            for tx, hint in llm_batch
        ]
        results = _categorize_via_llm(user_id, rows)
        results_by_id = {r.tx_id: r for r in results}
        for tx, _ in llm_batch:
            r = results_by_id.get(tx.id)
            if r is None:
                continue
            slug = r.category_slug if r.category_slug != "uncategorized" else "uncategorized"
            cat = cats_by_slug.get(slug, cats_by_slug["uncategorized"])
            tx.category_id = cat.id
            tx.category_source = "llm"
            tx.category_confidence = Decimal(str(r.confidence))
            resolved += 1
            # Cache only confident, slug-bearing results.
            # Guard against duplicate key if the same merchant appears in a
            # prior file that already populated the cache this session.
            if r.category_slug != "uncategorized":
                existing_cache = session.query(MerchantCategoryCache).filter_by(
                    user_id=user_id,
                    merchant_pattern=tx.merchant_normalized,
                    is_regex=False,
                ).first()
                if existing_cache is None:
                    session.add(MerchantCategoryCache(
                        user_id=user_id,
                        merchant_pattern=tx.merchant_normalized,
                        category_id=cat.id,
                        source="llm",
                        confidence=Decimal(str(r.confidence)),
                        hit_count=1,
                        last_hit_at=datetime.utcnow(),
                    ))
                else:
                    # Update in-place: keep the higher-confidence verdict.
                    if Decimal(str(r.confidence)) > existing_cache.confidence:
                        existing_cache.category_id = cat.id
                        existing_cache.confidence = Decimal(str(r.confidence))
                        existing_cache.last_hit_at = datetime.utcnow()

    session.flush()
    return resolved


def _extract_anaf_from_raw_row(raw_row_json: str) -> str | None:
    """Pull the Max 'ענף' field out of raw_row_json if present."""
    try:
        data = json.loads(raw_row_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        anaf = data.get("anaf")
        if isinstance(anaf, str) and anaf.strip():
            return anaf.strip()
    return None


def _categorize_via_llm(
    user_id: str, rows: list[CategorizeRow],
) -> list[CategorizeResult]:
    """Indirection seam — patched in unit tests."""
    agent = HouseholdCategorizerAgent(user_id=user_id)
    from argosy.services.expense_ingest.taxonomy_seed import DEFAULT_TAXONOMY
    taxonomy = [e.slug for e in DEFAULT_TAXONOMY]
    BATCH_SIZE = 50
    out: list[CategorizeResult] = []
    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i : i + BATCH_SIZE]
        out.extend(agent.categorize_batch(chunk, taxonomy))
    return out
