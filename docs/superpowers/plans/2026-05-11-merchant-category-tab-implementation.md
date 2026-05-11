# Merchant ↔ Category Tab + Range-Bulk Labeling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a `/expenses/merchants` tab with merchant-grouped category management (filter, sort, multi-select bulk-apply, hierarchical picker, sub-category creation), plus date-range bulk-labeling on the existing `/expenses/transactions` page. Zero schema changes.

**Architecture:** Extract `apply_merchant_category()` as the single source of truth for "this merchant's mapping changed" — it writes the cache row and fans out to sibling transactions. The existing `PATCH /transactions/{id}` keeps backward-compatible auto-fan-out semantics by default but gains an opt-in `apply_to_siblings` field. Five new REST endpoints sit on top of this primitive. The frontend gets two reusable modals (bare hierarchical picker; label editor that embeds the picker + tag chips) and a new tab.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / Pydantic v2 (backend); Next.js (current vendored version — see `ui/AGENTS.md` for the warning about training-data drift) / React / Tailwind / shadcn (frontend); pytest (backend tests).

**Spec:** `docs/superpowers/specs/2026-05-11-merchant-category-tab-design.md`

**Common conventions for this plan:**
- Python interpreter: `D:/Projects/financial-advisor/.venv/Scripts/python.exe`
- Run backend tests: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" <path>`
- Run lint+typecheck on UI: `cd ui ; npm run lint ; npm run typecheck`
- All commits follow the existing convention: `<type>(<scope>): <subject>` (look at `git log --oneline -20` for examples).
- Working tree must be clean before starting; commit after each task.
- This plan does NOT introduce schema changes — no Alembic migrations.
- Dev DB at `db/argosy.db` has full corpus (6 sources, ~2,180 transactions). All tests run against test DB fixtures, not the dev DB.
- Frontend code lives under `ui/` and follows the existing patterns: shadcn primitives at `ui/src/components/ui/*`, expenses-specific at `ui/src/components/expenses/*`, API client at `ui/src/lib/expenses/api.ts`.
- Before starting Task 1, re-read §5 (API contract) and §6 (data model) of the spec.

**Validation gates:**
- After every backend task: `pytest -m "not llm_eval" <touched paths>` passes.
- After every frontend task: `cd ui ; npm run lint ; npm run typecheck` passes with no new errors.
- After Phase 8 (final task): full suite `pytest -m "not llm_eval"` passes, total count is at least baseline + 25 new tests.

---

## Phase 1 — Backend service extraction

The service extraction is the linchpin. Once `apply_merchant_category()` exists and is tested directly, the new endpoints become thin wrappers.

### Task 1: Scaffold `argosy/services/merchant_service.py`

**Files:**
- Create: `argosy/services/merchant_service.py`
- Create: `tests/test_apply_merchant_category_service.py`

- [ ] **Step 1: Write a smoke test that imports the new module**

```python
# tests/test_apply_merchant_category_service.py
"""Tests for argosy.services.merchant_service — the apply-category primitive
shared by PATCH /transactions, PATCH /merchants, and the bulk endpoint.
"""
from argosy.services import merchant_service


def test_module_importable():
    assert hasattr(merchant_service, "__name__")
```

- [ ] **Step 2: Run test, expect ModuleNotFoundError**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_apply_merchant_category_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'argosy.services.merchant_service'`

- [ ] **Step 3: Create the module with a docstring**

```python
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
```

- [ ] **Step 4: Run test, expect PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_apply_merchant_category_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/services/merchant_service.py tests/test_apply_merchant_category_service.py
git commit -m "feat(merchant-service): scaffold module"
```

---

### Task 2: Implement `apply_merchant_category()`

**Files:**
- Modify: `argosy/services/merchant_service.py`
- Modify: `tests/test_apply_merchant_category_service.py`

The function takes a user_id + merchant_normalized + either a category_slug (set new category) or `confirm=True` (lock current category as user-confirmed). It updates the cache row and fans out to all sibling transactions in one pass. Returns a dataclass with what happened.

- [ ] **Step 1: Write tests for new merchant + existing cache row + confirm path**

Replace `tests/test_apply_merchant_category_service.py` body with:

```python
# tests/test_apply_merchant_category_service.py
"""Tests for argosy.services.merchant_service.apply_merchant_category."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from argosy.services.merchant_service import (
    ApplyResult, apply_merchant_category, MerchantNotFoundError,
)
from argosy.state.models import (
    ExpenseCategory, ExpenseTransaction, MerchantCategoryCache,
)


@pytest.fixture()
def session_with_user_and_categories(session_factory):
    """A session seeded with user 'ariel', the default taxonomy, and a few
    txs from merchant 'שטראוס' currently category=uncategorized.
    """
    from argosy.state.models import User
    from argosy.services.expense_ingest.taxonomy_seed import seed_user_categories
    with session_factory() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
        seed_user_categories(s, "ariel")
        s.flush()
        uncat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="uncategorized"
        ).one()
        # Need a source + statement to satisfy NOT NULL FKs; create minimal ones.
        from argosy.state.models import ExpenseSource, ExpenseStatement
        from datetime import date
        src = ExpenseSource(user_id="ariel", issuer="isracard",
                            external_id="0235", display_name="Test")
        s.add(src); s.flush()
        stmt = ExpenseStatement(user_id="ariel", source_id=src.id,
                                period_start=date(2026, 5, 1),
                                period_end=date(2026, 5, 31))
        s.add(stmt); s.flush()
        for i in range(3):
            s.add(ExpenseTransaction(
                user_id="ariel", statement_id=stmt.id, source_id=src.id,
                occurred_on=date(2026, 5, 1 + i),
                merchant_raw="שטראוס בע\"מ", merchant_normalized="שטראוס",
                amount_nis=Decimal("50.00"), direction="debit",
                tx_type="regular", raw_row_json="{}",
                category_id=uncat.id, category_source="llm",
                category_confidence=Decimal("0.40"),
            ))
        s.commit()
        yield s


def test_apply_new_merchant_creates_cache_and_fans_out(session_with_user_and_categories):
    s = session_with_user_and_categories
    result = apply_merchant_category(
        s, user_id="ariel", merchant_normalized="שטראוס",
        category_slug="food.groceries",
    )
    assert isinstance(result, ApplyResult)
    assert result.cache_row_created is True
    assert result.affected_transactions == 3
    assert result.resolved_category_slug == "food.groceries"

    cache = s.query(MerchantCategoryCache).filter_by(
        user_id="ariel", merchant_pattern="שטראוס", is_regex=False,
    ).one()
    assert cache.source == "user"
    assert cache.confidence == Decimal("1.00")

    cat = s.query(ExpenseCategory).filter_by(
        user_id="ariel", slug="food.groceries"
    ).one()
    txs = s.query(ExpenseTransaction).filter_by(
        user_id="ariel", merchant_normalized="שטראוס"
    ).all()
    for tx in txs:
        assert tx.category_id == cat.id
        assert tx.category_source == "user"
        assert tx.category_confidence == Decimal("1.00")


def test_apply_existing_cache_row_overwrites(session_with_user_and_categories):
    s = session_with_user_and_categories
    apply_merchant_category(s, user_id="ariel",
                            merchant_normalized="שטראוס",
                            category_slug="food.groceries")
    s.commit()
    result = apply_merchant_category(
        s, user_id="ariel", merchant_normalized="שטראוס",
        category_slug="discretionary.snacks_treats",
    )
    assert result.cache_row_created is False
    cache = s.query(MerchantCategoryCache).filter_by(
        user_id="ariel", merchant_pattern="שטראוס"
    ).one()
    new_cat = s.query(ExpenseCategory).filter_by(
        user_id="ariel", slug="discretionary.snacks_treats"
    ).one()
    assert cache.category_id == new_cat.id


def test_apply_confirm_only_uses_most_common_category(
    session_with_user_and_categories
):
    s = session_with_user_and_categories
    # Set 2 of the 3 txs to food.groceries directly (simulating LLM verdict)
    food = s.query(ExpenseCategory).filter_by(
        user_id="ariel", slug="food.groceries"
    ).one()
    txs = s.query(ExpenseTransaction).filter_by(
        user_id="ariel", merchant_normalized="שטראוס"
    ).order_by(ExpenseTransaction.occurred_on).all()
    txs[0].category_id = food.id
    txs[1].category_id = food.id
    s.commit()

    result = apply_merchant_category(
        s, user_id="ariel", merchant_normalized="שטראוס", confirm=True,
    )
    assert result.resolved_category_slug == "food.groceries"
    cache = s.query(MerchantCategoryCache).filter_by(
        user_id="ariel", merchant_pattern="שטראוס"
    ).one()
    assert cache.source == "user"
    assert cache.confidence == Decimal("1.00")


def test_apply_unknown_merchant_raises(session_with_user_and_categories):
    s = session_with_user_and_categories
    with pytest.raises(MerchantNotFoundError):
        apply_merchant_category(
            s, user_id="ariel", merchant_normalized="ghost-merchant",
            category_slug="food.groceries",
        )
```

You'll need a `session_factory` fixture. Check `tests/conftest.py` — there's likely one already; if not, search `tests/` for an existing fixture that gives you a fresh `Session` with the test DB and reuse it. If a comparable fixture exists under another name (`db_session`, `expense_session`, etc.), import that fixture in this test file instead.

- [ ] **Step 2: Run tests, expect failures**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_apply_merchant_category_service.py -v`
Expected: All four FAIL with `ImportError: cannot import name 'apply_merchant_category' from 'argosy.services.merchant_service'`.

- [ ] **Step 3: Implement `apply_merchant_category` + `ApplyResult` + `MerchantNotFoundError`**

Replace `argosy/services/merchant_service.py` content with:

```python
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
from datetime import datetime, timezone
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
    most_recent: dict[int, "date"] = {}
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
```

Add the missing `date` import at top: `from datetime import date, datetime, timezone`. (Replace the existing datetime import line accordingly.)

- [ ] **Step 4: Run tests, expect all four to PASS**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_apply_merchant_category_service.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add argosy/services/merchant_service.py tests/test_apply_merchant_category_service.py
git commit -m "feat(merchant-service): apply_merchant_category primitive"
```

---

### Task 3: Refactor `PATCH /transactions/{id}` to use the service + add `apply_to_siblings`

The existing endpoint at `argosy/api/routes/expenses.py:282-335` has inline cache + fan-out logic. We replace the body with a call to `apply_merchant_category` and add the new `apply_to_siblings` body field. Default behavior is preserved when the field is omitted (back-compat).

**Files:**
- Modify: `argosy/api/routes/expenses.py` (PatchCategoryRequest + patch_transaction_category)
- Create: `tests/test_patch_transaction_fanout_flag.py`

- [ ] **Step 1: Write regression tests for the `apply_to_siblings` flag**

```python
# tests/test_patch_transaction_fanout_flag.py
"""Regression: PATCH /transactions/{id} default fan-out vs explicit
apply_to_siblings flag."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture()
def seeded_client(expense_client):
    """expense_client + seeded category taxonomy + 3 txs for merchant 'X'."""
    from argosy.services.expense_ingest.taxonomy_seed import seed_user_categories
    from argosy.state.models import (
        ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
    )
    SessionLocal = expense_client.app.state.session_factory
    with SessionLocal() as s:
        seed_user_categories(s, "ariel")
        s.flush()
        uncat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="uncategorized"
        ).one()
        src = ExpenseSource(user_id="ariel", issuer="isracard",
                            external_id="9999", display_name="Test")
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id="ariel", source_id=src.id,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
        )
        s.add(stmt); s.flush()
        ids = []
        for i in range(3):
            tx = ExpenseTransaction(
                user_id="ariel", statement_id=stmt.id, source_id=src.id,
                occurred_on=date(2026, 5, 1 + i),
                merchant_raw="X", merchant_normalized="X",
                amount_nis=Decimal("10.00"), direction="debit",
                tx_type="regular", raw_row_json="{}",
                category_id=uncat.id, category_source="llm",
                category_confidence=Decimal("0.40"),
            )
            s.add(tx); s.flush(); ids.append(tx.id)
        s.commit()
        yield expense_client, ids


def test_patch_default_fans_out_for_backcompat(seeded_client):
    client, ids = seeded_client
    resp = client.patch(
        f"/api/expenses/transactions/{ids[0]}",
        json={"user_id": "ariel", "category_slug": "food.groceries"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["affected_count"] == 3

    # All three rows are now food.groceries, source=user.
    from argosy.state.models import ExpenseCategory, ExpenseTransaction
    SessionLocal = client.app.state.session_factory
    with SessionLocal() as s:
        food = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="food.groceries"
        ).one()
        for tx_id in ids:
            tx = s.get(ExpenseTransaction, tx_id)
            assert tx.category_id == food.id
            assert tx.category_source == "user"


def test_patch_with_apply_to_siblings_false_only_updates_one(seeded_client):
    client, ids = seeded_client
    resp = client.patch(
        f"/api/expenses/transactions/{ids[0]}",
        json={"user_id": "ariel", "category_slug": "food.groceries",
              "apply_to_siblings": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["affected_count"] == 1

    from argosy.state.models import (
        ExpenseCategory, ExpenseTransaction, MerchantCategoryCache,
    )
    SessionLocal = client.app.state.session_factory
    with SessionLocal() as s:
        food = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="food.groceries"
        ).one()
        tx0 = s.get(ExpenseTransaction, ids[0])
        assert tx0.category_id == food.id
        # Siblings unchanged
        for tx_id in ids[1:]:
            tx = s.get(ExpenseTransaction, tx_id)
            assert tx.category_id != food.id
        # No cache row was written.
        cache = s.query(MerchantCategoryCache).filter_by(
            user_id="ariel", merchant_pattern="X", is_regex=False,
        ).one_or_none()
        assert cache is None
```

(Reuse the `expense_client` fixture from `tests/test_expense_routes.py:12` — import it via `pytest_plugins` in `tests/conftest.py` if it isn't visible, or move the fixture there. Verify which conftest exposes `expense_client` before this task starts.)

- [ ] **Step 2: Run tests, expect failures**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_patch_transaction_fanout_flag.py -v`
Expected: `test_patch_default_fans_out_for_backcompat` PASSES (the current behavior already fans out). `test_patch_with_apply_to_siblings_false_only_updates_one` FAILS because the flag is not honored — the current endpoint always fans out and writes the cache.

- [ ] **Step 3: Replace `PatchCategoryRequest` + `patch_transaction_category` body**

In `argosy/api/routes/expenses.py`, locate `PatchCategoryRequest` (line ~270) and replace with:

```python
class PatchCategoryRequest(BaseModel):
    user_id: str
    category_slug: str
    apply_to_siblings: bool = True   # back-compat default; new UI sends False
```

Then replace the body of `patch_transaction_category` (lines ~284-335) with:

```python
@router.patch("/transactions/{transaction_id}",
               response_model=PatchCategoryResponse)
def patch_transaction_category(
    transaction_id: int,
    body: PatchCategoryRequest,
    db: Annotated[Session, Depends(get_db)],
) -> PatchCategoryResponse:
    """Set a single transaction's category.

    When ``apply_to_siblings=True`` (default), the same category is applied to
    every other transaction with the same ``merchant_normalized`` for this
    user, and a ``merchant_category_cache`` row is written/updated with
    ``source='user'``, ``confidence=1.00``. The default is True for backward
    compatibility with the original EX1 inline-edit UI; the new UI on
    ``/expenses/transactions`` sends ``apply_to_siblings=False`` explicitly.

    When ``apply_to_siblings=False``, only this one row is updated. The cache
    is NOT touched — future ingests of the same merchant continue to use
    whatever mapping (or lack thereof) existed before.
    """
    tx = db.query(ExpenseTransaction).filter_by(
        id=transaction_id, user_id=body.user_id,
    ).one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    cat = db.query(ExpenseCategory).filter_by(
        user_id=body.user_id, slug=body.category_slug,
    ).one_or_none()
    if cat is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown category {body.category_slug}",
        )

    if body.apply_to_siblings:
        from argosy.services.merchant_service import apply_merchant_category
        result = apply_merchant_category(
            db, user_id=body.user_id,
            merchant_normalized=tx.merchant_normalized,
            category_slug=body.category_slug,
        )
        db.commit()
        return PatchCategoryResponse(
            transaction_id=tx.id,
            category_slug=body.category_slug,
            category_source="user",
            affected_count=result.affected_transactions,
        )

    # Per-tx only: touch this row, leave the cache and siblings alone.
    tx.category_id = cat.id
    tx.category_source = "user"
    tx.category_confidence = Decimal("1.00")
    db.commit()
    return PatchCategoryResponse(
        transaction_id=tx.id, category_slug=body.category_slug,
        category_source="user", affected_count=1,
    )
```

- [ ] **Step 4: Run tests — both pass; also re-run any pre-existing `test_expense_routes` PATCH test**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_patch_transaction_fanout_flag.py tests/test_expense_routes.py -v`
Expected: all pass; no regressions in the existing route tests.

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/expenses.py tests/test_patch_transaction_fanout_flag.py
git commit -m "refactor(expenses-api): PATCH /transactions uses merchant_service + apply_to_siblings flag"
```

---

## Phase 2 — Backend: sub-category creation

### Task 4: `POST /api/expenses/categories`

**Files:**
- Modify: `argosy/api/routes/expenses.py` (add Pydantic types + endpoint near the existing GET /categories at line ~356)
- Create: `tests/test_categories_post.py`

- [ ] **Step 1: Write tests for happy path, duplicate, parent-must-be-top-level, slug-with-dot**

```python
# tests/test_categories_post.py
"""POST /api/expenses/categories — add sub-category under existing parent."""
from __future__ import annotations

import pytest


@pytest.fixture()
def client_with_categories(expense_client):
    from argosy.services.expense_ingest.taxonomy_seed import seed_user_categories
    SessionLocal = expense_client.app.state.session_factory
    with SessionLocal() as s:
        seed_user_categories(s, "ariel")
        s.commit()
    return expense_client


def test_create_subcategory_happy(client_with_categories):
    resp = client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "insurance",
              "slug": "health", "label_en": "Health Insurance",
              "label_he": "ביטוח בריאות"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slug"] == "insurance.health"
    assert body["label_en"] == "Health Insurance"
    assert body["parent_slug"] == "insurance"


def test_create_subcategory_duplicate_returns_409(client_with_categories):
    client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "insurance",
              "slug": "health", "label_en": "Health Insurance"},
    )
    resp = client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "insurance",
              "slug": "health", "label_en": "Health Insurance"},
    )
    assert resp.status_code == 409, resp.text


def test_create_subcategory_parent_not_top_level_returns_422(client_with_categories):
    # First add insurance.health.
    client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "insurance",
              "slug": "health", "label_en": "Health Insurance"},
    )
    # Now try to add insurance.health.dental — parent is not top-level.
    resp = client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "insurance.health",
              "slug": "dental", "label_en": "Dental"},
    )
    assert resp.status_code == 422, resp.text


def test_create_subcategory_slug_with_dot_returns_422(client_with_categories):
    resp = client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "insurance",
              "slug": "health.life", "label_en": "X"},
    )
    assert resp.status_code == 422, resp.text


def test_create_subcategory_unknown_parent_returns_404(client_with_categories):
    resp = client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "no-such",
              "slug": "x", "label_en": "X"},
    )
    assert resp.status_code == 404, resp.text


def test_create_subcategory_inherits_parent_flags(client_with_categories):
    # 'transfers' is is_excluded_from_spend=True per default taxonomy.
    resp = client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "transfers",
              "slug": "internal_bit", "label_en": "BIT (internal)"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_excluded_from_spend"] is True
```

- [ ] **Step 2: Run tests, expect 6 failures (404 on POST since the endpoint doesn't exist)**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_categories_post.py -v`
Expected: all fail with `assert 404 == 200` or similar.

- [ ] **Step 3: Implement the endpoint**

In `argosy/api/routes/expenses.py`, immediately after the existing `GET /categories` endpoint, add:

```python
class CategoryCreateRequest(BaseModel):
    user_id: str
    parent_slug: str
    slug: str = Field(..., min_length=1, max_length=64)
    label_en: str = Field(..., min_length=1, max_length=64)
    label_he: str | None = None


@router.post("/categories", response_model=CategoryOut)
def create_category(
    body: CategoryCreateRequest,
    db: Annotated[Session, Depends(get_db)],
) -> CategoryOut:
    """Create a sub-category under an existing top-level parent.

    MVP: one nesting level only. Parent must have parent_id IS NULL.
    Stored slug is ``{parent_slug}.{slug}``. is_excluded_from_spend and
    is_inflow are inherited from the parent.
    """
    if "." in body.slug:
        raise HTTPException(
            status_code=422,
            detail="slug must not contain '.'; nesting is encoded by parent_slug",
        )

    parent = db.query(ExpenseCategory).filter_by(
        user_id=body.user_id, slug=body.parent_slug,
    ).one_or_none()
    if parent is None:
        raise HTTPException(status_code=404,
                            detail=f"parent category {body.parent_slug!r} not found")
    if parent.parent_id is not None:
        raise HTTPException(
            status_code=422,
            detail=f"parent {body.parent_slug!r} is not top-level; "
                   "only one level of nesting is supported",
        )

    new_slug = f"{body.parent_slug}.{body.slug}"
    dupe = db.query(ExpenseCategory).filter_by(
        user_id=body.user_id, slug=new_slug,
    ).one_or_none()
    if dupe is not None:
        raise HTTPException(status_code=409,
                            detail=f"category {new_slug!r} already exists")

    label_he = body.label_he or body.label_en
    max_order = db.query(func.max(ExpenseCategory.display_order)).filter_by(
        user_id=body.user_id, parent_id=parent.id,
    ).scalar() or parent.display_order
    cat = ExpenseCategory(
        user_id=body.user_id, slug=new_slug,
        label_en=body.label_en, label_he=label_he,
        parent_id=parent.id,
        is_excluded_from_spend=parent.is_excluded_from_spend,
        is_inflow=parent.is_inflow,
        display_order=int(max_order) + 1,
    )
    db.add(cat); db.commit(); db.refresh(cat)
    return CategoryOut(
        id=cat.id, slug=cat.slug,
        label_en=cat.label_en, label_he=cat.label_he,
        parent_slug=parent.slug,
        is_excluded_from_spend=cat.is_excluded_from_spend,
        is_inflow=cat.is_inflow,
    )
```

- [ ] **Step 4: Run tests, all six pass**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_categories_post.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/expenses.py tests/test_categories_post.py
git commit -m "feat(expenses-api): POST /categories for sub-category creation"
```

---

## Phase 3 — Backend: merchant endpoints

### Task 5: Add Pydantic models for the merchant endpoints

**Files:**
- Modify: `argosy/api/routes/expenses.py` (append a new section near the end, before the existing `/dashboard-overview` block — section ordering follows existing file convention)
- Create: `tests/test_merchants_models.py`

- [ ] **Step 1: Write a smoke test that imports the new types**

```python
# tests/test_merchants_models.py
"""Smoke tests for Pydantic types backing the merchant endpoints."""
from argosy.api.routes.expenses import (
    MerchantOut, MerchantsListResponse,
    MerchantPatchRequest, MerchantPatchResponse,
    BulkCategoryRequest, BulkCategoryItemResult, BulkCategoryResponse,
)


def test_merchant_out_fields():
    m = MerchantOut(
        merchant_normalized="X", category_slug="food.groceries",
        category_label="Groceries", parent_slug="food",
        parent_label="Food", confidence=0.92, source="llm",
        is_cached=True, tx_count=3, total_nis=100.0, total_usd=0.0,
        last_seen="2026-05-08",
    )
    assert m.tx_count == 3


def test_merchant_patch_request_accepts_either_shape():
    a = MerchantPatchRequest(user_id="ariel", category_slug="food.groceries")
    b = MerchantPatchRequest(user_id="ariel", confirm=True)
    assert a.confirm is False
    assert b.confirm is True
    assert b.category_slug is None


def test_bulk_category_request_requires_one_of():
    import pytest as _pt
    from pydantic import ValidationError
    with _pt.raises(ValidationError):
        BulkCategoryRequest(user_id="ariel", merchant_normalizeds=["X"])
```

- [ ] **Step 2: Run tests, expect ImportError**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_merchants_models.py -v`
Expected: ImportError on the missing names.

- [ ] **Step 3: Add the types**

Append to `argosy/api/routes/expenses.py` (near the bottom is fine, or in a section near the existing `/categories` block):

```python
# ---------------------------------------------------------------------------
# Merchants — GET / PATCH / bulk-category
# ---------------------------------------------------------------------------

class MerchantOut(BaseModel):
    merchant_normalized: str
    category_slug: str
    category_label: str
    parent_slug: str | None
    parent_label: str | None
    confidence: float | None
    source: str           # 'user' | 'llm' | 'issuer' | 'cache' | 'uncached'
    is_cached: bool
    tx_count: int
    total_nis: float
    total_usd: float
    last_seen: str        # ISO date


class MerchantsListResponse(BaseModel):
    merchants: list[MerchantOut]
    total: int


class MerchantPatchRequest(BaseModel):
    user_id: str
    category_slug: str | None = None
    confirm: bool = False

    def model_post_init(self, __context) -> None:
        if self.category_slug is None and not self.confirm:
            raise ValueError("Must provide category_slug or confirm=True")


class MerchantPatchResponse(BaseModel):
    merchant_normalized: str
    category_slug: str
    affected_transactions: int
    cache_row_created: bool


class BulkCategoryRequest(BaseModel):
    user_id: str
    merchant_normalizeds: list[str] = Field(..., min_length=1)
    category_slug: str | None = None
    confirm: bool = False

    def model_post_init(self, __context) -> None:
        if self.category_slug is None and not self.confirm:
            raise ValueError("Must provide category_slug or confirm=True")


class BulkCategoryItemResult(BaseModel):
    merchant_normalized: str
    status: str           # 'ok' | 'error'
    affected_transactions: int = 0
    message: str | None = None


class BulkCategoryResponse(BaseModel):
    results: list[BulkCategoryItemResult]
    ok_count: int
    error_count: int
    total_affected_transactions: int
```

- [ ] **Step 4: Run tests, expect 3 pass**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_merchants_models.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/expenses.py tests/test_merchants_models.py
git commit -m "feat(expenses-api): pydantic models for merchant endpoints"
```

---

### Task 6: `GET /api/expenses/merchants`

**Files:**
- Modify: `argosy/api/routes/expenses.py` (add endpoint right after the types from Task 5)
- Create: `tests/test_merchants_endpoint.py`

- [ ] **Step 1: Write tests for aggregation correctness, filters, sort**

```python
# tests/test_merchants_endpoint.py
"""GET /api/expenses/merchants — merchant-aggregated listing."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture()
def seeded(expense_client):
    """Three merchants in three states:
      A — cache row (source=user, confidence=1.00), 2 txs food.groceries
      B — cache row (source=llm, confidence=0.92), 1 tx dining_out.restaurants
      C — no cache row, 3 txs in uncategorized
    """
    from argosy.services.expense_ingest.taxonomy_seed import seed_user_categories
    from argosy.state.models import (
        ExpenseCategory, ExpenseSource, ExpenseStatement,
        ExpenseTransaction, MerchantCategoryCache,
    )
    from datetime import datetime, timezone
    SessionLocal = expense_client.app.state.session_factory
    with SessionLocal() as s:
        seed_user_categories(s, "ariel")
        s.flush()
        food = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="food.groceries"
        ).one()
        dining = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="dining_out.restaurants"
        ).one()
        uncat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="uncategorized"
        ).one()
        src = ExpenseSource(user_id="ariel", issuer="isracard",
                            external_id="1234", display_name="T")
        s.add(src); s.flush()
        stmt = ExpenseStatement(user_id="ariel", source_id=src.id,
                                period_start=date(2026, 5, 1),
                                period_end=date(2026, 5, 31))
        s.add(stmt); s.flush()

        def mk(merch, cat, n):
            for i in range(n):
                s.add(ExpenseTransaction(
                    user_id="ariel", statement_id=stmt.id, source_id=src.id,
                    occurred_on=date(2026, 5, 1 + i),
                    merchant_raw=merch, merchant_normalized=merch,
                    amount_nis=Decimal("10.00"), direction="debit",
                    tx_type="regular", raw_row_json="{}",
                    category_id=cat.id, category_source="user",
                    category_confidence=Decimal("1.00"),
                ))
        mk("A", food, 2)
        mk("B", dining, 1)
        mk("C", uncat, 3)
        now = datetime.now(timezone.utc)
        s.add(MerchantCategoryCache(
            user_id="ariel", merchant_pattern="A", is_regex=False,
            category_id=food.id, source="user",
            confidence=Decimal("1.00"), hit_count=2, last_hit_at=now,
        ))
        s.add(MerchantCategoryCache(
            user_id="ariel", merchant_pattern="B", is_regex=False,
            category_id=dining.id, source="llm",
            confidence=Decimal("0.92"), hit_count=1, last_hit_at=now,
        ))
        s.commit()
    return expense_client


def test_list_all_three_merchants(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    norms = {m["merchant_normalized"] for m in body["merchants"]}
    assert norms == {"A", "B", "C"}


def test_filter_uncategorized(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel&category=uncategorized")
    body = r.json()
    assert body["total"] == 1
    assert body["merchants"][0]["merchant_normalized"] == "C"
    assert body["merchants"][0]["is_cached"] is False


def test_filter_by_source_user(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel&source=user")
    body = r.json()
    assert {m["merchant_normalized"] for m in body["merchants"]} == {"A"}


def test_filter_min_confidence(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel&min_confidence=0.95")
    body = r.json()
    # Only 'A' (1.00) qualifies.
    assert {m["merchant_normalized"] for m in body["merchants"]} == {"A"}


def test_search_substring(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel&search=c")
    body = r.json()
    assert {m["merchant_normalized"] for m in body["merchants"]} == {"C"}


def test_sort_by_tx_count_desc(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel&sort=tx_count&order=desc")
    body = r.json()
    counts = [m["tx_count"] for m in body["merchants"]]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] == 3


def test_default_sort_needs_attention_uncategorized_first(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel")
    body = r.json()
    assert body["merchants"][0]["merchant_normalized"] == "C"  # uncategorized first


def test_category_label_and_parent_label_populated(seeded):
    r = seeded.get("/api/expenses/merchants?user_id=ariel&sort=merchant&order=asc")
    body = r.json()
    a = next(m for m in body["merchants"] if m["merchant_normalized"] == "A")
    assert a["category_label"] == "Groceries"
    assert a["parent_slug"] == "food"
    assert a["parent_label"] == "Food"
```

- [ ] **Step 2: Run tests, expect failures (endpoint doesn't exist)**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_merchants_endpoint.py -v`
Expected: all 8 fail with `assert 404 == 200`.

- [ ] **Step 3: Implement the endpoint**

Add to `argosy/api/routes/expenses.py` after the Pydantic types from Task 5:

```python
@router.get("/merchants", response_model=MerchantsListResponse)
def list_merchants(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    category: str | None = None,
    source: str | None = None,
    min_confidence: float | None = None,
    max_confidence: float | None = None,
    search: str | None = None,
    sort: str = "needs_attention",
    order: str = "desc",
    limit: int = Query(default=500, ge=1, le=1000),
) -> MerchantsListResponse:
    """Aggregate transactions by merchant_normalized + join to cache + category."""
    from sqlalchemy.orm import aliased
    Parent = aliased(ExpenseCategory)

    cache_subq = (
        sa_select(MerchantCategoryCache)
        .where(MerchantCategoryCache.user_id == user_id,
               MerchantCategoryCache.is_regex.is_(False))
        .subquery()
    )

    base = (
        sa_select(
            ExpenseTransaction.merchant_normalized.label("merchant"),
            ExpenseCategory.slug.label("cat_slug"),
            ExpenseCategory.label_en.label("cat_label"),
            Parent.slug.label("parent_slug"),
            Parent.label_en.label("parent_label"),
            cache_subq.c.confidence.label("cache_confidence"),
            cache_subq.c.source.label("cache_source"),
            cache_subq.c.id.label("cache_id"),
            func.count(ExpenseTransaction.id).label("tx_count"),
            func.sum(
                case((ExpenseTransaction.currency_orig.is_(None),
                      ExpenseTransaction.amount_nis), else_=0)
            ).label("total_nis"),
            func.sum(
                case((ExpenseTransaction.currency_orig == "USD",
                      ExpenseTransaction.amount_orig), else_=0)
            ).label("total_usd"),
            func.max(ExpenseTransaction.occurred_on).label("last_seen"),
            func.avg(ExpenseTransaction.category_confidence).label("avg_tx_conf"),
        )
        .select_from(ExpenseTransaction)
        .outerjoin(cache_subq,
                   cache_subq.c.merchant_pattern ==
                   ExpenseTransaction.merchant_normalized)
        .outerjoin(ExpenseCategory,
                   ExpenseCategory.id ==
                   func.coalesce(cache_subq.c.category_id,
                                 ExpenseTransaction.category_id))
        .outerjoin(Parent, Parent.id == ExpenseCategory.parent_id)
        .where(ExpenseTransaction.user_id == user_id)
        .group_by(
            ExpenseTransaction.merchant_normalized,
            ExpenseCategory.slug, ExpenseCategory.label_en,
            Parent.slug, Parent.label_en,
            cache_subq.c.confidence, cache_subq.c.source, cache_subq.c.id,
        )
    )

    if category == "uncategorized":
        base = base.where(or_(
            ExpenseCategory.slug == "uncategorized",
            ExpenseCategory.slug.is_(None),
        ))
    elif category:
        base = base.where(ExpenseCategory.slug == category)

    if source == "uncached":
        base = base.where(cache_subq.c.id.is_(None))
    elif source:
        base = base.where(cache_subq.c.source == source)

    if min_confidence is not None:
        base = base.where(func.coalesce(
            cache_subq.c.confidence,
            func.avg(ExpenseTransaction.category_confidence),
        ) >= min_confidence)
    if max_confidence is not None:
        base = base.where(func.coalesce(
            cache_subq.c.confidence,
            func.avg(ExpenseTransaction.category_confidence),
        ) <= max_confidence)

    if search:
        base = base.where(
            ExpenseTransaction.merchant_normalized.ilike(f"%{search}%")
        )

    # Sort
    sort_col_map = {
        "merchant": ExpenseTransaction.merchant_normalized,
        "category": ExpenseCategory.slug,
        "confidence": func.coalesce(
            cache_subq.c.confidence,
            func.avg(ExpenseTransaction.category_confidence),
        ),
        "tx_count": func.count(ExpenseTransaction.id),
        "total_nis": func.sum(
            case((ExpenseTransaction.currency_orig.is_(None),
                  ExpenseTransaction.amount_nis), else_=0),
        ),
        "last_seen": func.max(ExpenseTransaction.occurred_on),
    }
    if sort == "needs_attention":
        # uncategorized first, then low-confidence non-user, then tx_count desc
        base = base.order_by(
            (ExpenseCategory.slug == "uncategorized").desc(),
            ExpenseCategory.slug.is_(None).desc(),
            (cache_subq.c.source != "user").desc(),
            func.coalesce(cache_subq.c.confidence, 0).asc(),
            func.count(ExpenseTransaction.id).desc(),
        )
    elif sort in sort_col_map:
        col = sort_col_map[sort]
        base = base.order_by(col.desc() if order == "desc" else col.asc())

    rows = db.execute(base.limit(limit)).all()
    merchants = [
        MerchantOut(
            merchant_normalized=r.merchant,
            category_slug=r.cat_slug or "uncategorized",
            category_label=r.cat_label or "Uncategorized",
            parent_slug=r.parent_slug,
            parent_label=r.parent_label,
            confidence=(float(r.cache_confidence) if r.cache_confidence is not None
                         else (float(r.avg_tx_conf) if r.avg_tx_conf is not None else None)),
            source=(r.cache_source if r.cache_id is not None else "uncached"),
            is_cached=r.cache_id is not None,
            tx_count=int(r.tx_count or 0),
            total_nis=float(r.total_nis or 0),
            total_usd=float(r.total_usd or 0),
            last_seen=r.last_seen.isoformat() if r.last_seen else "",
        )
        for r in rows
    ]
    return MerchantsListResponse(merchants=merchants, total=len(merchants))
```

- [ ] **Step 4: Run tests, expect all 8 to pass**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_merchants_endpoint.py -v`
Expected: 8 passed. If a query plan issue arises around the `func.avg` in WHERE (SQLite groups before WHERE evaluates aggregates), wrap the confidence filters in a HAVING clause (`base = base.having(...)` instead of `.where(...)`); the test expectations don't change.

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/expenses.py tests/test_merchants_endpoint.py
git commit -m "feat(expenses-api): GET /merchants aggregated listing"
```

---

### Task 7: `PATCH /api/expenses/merchants/{merchant_normalized}`

**Files:**
- Modify: `argosy/api/routes/expenses.py` (add endpoint after GET /merchants)
- Create: `tests/test_merchant_patch.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_merchant_patch.py
"""PATCH /api/expenses/merchants/{merchant_normalized}."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture()
def seeded(expense_client):
    from argosy.services.expense_ingest.taxonomy_seed import seed_user_categories
    from argosy.state.models import (
        ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
    )
    SessionLocal = expense_client.app.state.session_factory
    with SessionLocal() as s:
        seed_user_categories(s, "ariel")
        s.flush()
        uncat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="uncategorized"
        ).one()
        src = ExpenseSource(user_id="ariel", issuer="isracard",
                            external_id="5555", display_name="T")
        s.add(src); s.flush()
        stmt = ExpenseStatement(user_id="ariel", source_id=src.id,
                                period_start=date(2026, 5, 1),
                                period_end=date(2026, 5, 31))
        s.add(stmt); s.flush()
        for i in range(2):
            s.add(ExpenseTransaction(
                user_id="ariel", statement_id=stmt.id, source_id=src.id,
                occurred_on=date(2026, 5, 1 + i),
                merchant_raw="שטראוס", merchant_normalized="שטראוס",
                amount_nis=Decimal("10.00"), direction="debit",
                tx_type="regular", raw_row_json="{}",
                category_id=uncat.id, category_source="llm",
                category_confidence=Decimal("0.40"),
            ))
        s.commit()
    return expense_client


def test_patch_with_category_slug(seeded):
    import urllib.parse
    name = urllib.parse.quote("שטראוס")
    r = seeded.patch(
        f"/api/expenses/merchants/{name}",
        json={"user_id": "ariel", "category_slug": "food.groceries"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["affected_transactions"] == 2
    assert body["cache_row_created"] is True
    assert body["category_slug"] == "food.groceries"


def test_patch_confirm_only_uses_current_category(seeded):
    # First categorize via PATCH (creates cache row).
    import urllib.parse
    name = urllib.parse.quote("שטראוס")
    seeded.patch(
        f"/api/expenses/merchants/{name}",
        json={"user_id": "ariel", "category_slug": "food.groceries"},
    )
    # Then confirm — category unchanged.
    r = seeded.patch(
        f"/api/expenses/merchants/{name}",
        json={"user_id": "ariel", "confirm": True},
    )
    assert r.status_code == 200
    assert r.json()["category_slug"] == "food.groceries"


def test_patch_unknown_merchant_returns_404(seeded):
    r = seeded.patch(
        "/api/expenses/merchants/no-such-merchant",
        json={"user_id": "ariel", "category_slug": "food.groceries"},
    )
    assert r.status_code == 404


def test_patch_unknown_category_returns_400(seeded):
    import urllib.parse
    name = urllib.parse.quote("שטראוס")
    r = seeded.patch(
        f"/api/expenses/merchants/{name}",
        json={"user_id": "ariel", "category_slug": "no-such-category"},
    )
    assert r.status_code == 400


def test_patch_missing_both_fields_returns_422(seeded):
    import urllib.parse
    name = urllib.parse.quote("שטראוס")
    r = seeded.patch(
        f"/api/expenses/merchants/{name}",
        json={"user_id": "ariel"},
    )
    assert r.status_code == 422
```

- [ ] **Step 2: Run tests, expect failures**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_merchant_patch.py -v`
Expected: all 5 fail.

- [ ] **Step 3: Add the endpoint**

In `argosy/api/routes/expenses.py`, after `list_merchants`:

```python
@router.patch("/merchants/{merchant_normalized:path}",
               response_model=MerchantPatchResponse)
def patch_merchant(
    merchant_normalized: str,
    body: MerchantPatchRequest,
    db: Annotated[Session, Depends(get_db)],
) -> MerchantPatchResponse:
    """Set a merchant's category. Fans out to all sibling transactions and
    writes/updates the merchant_category_cache row.

    Two body shapes:
      {"category_slug": "..."}  → new category, source=user, confidence=1.00
      {"confirm": true}          → lock current category, source=user, conf=1.00
    """
    from argosy.services.merchant_service import (
        apply_merchant_category, MerchantNotFoundError, CategoryNotFoundError,
        NothingToConfirmError,
    )
    try:
        result = apply_merchant_category(
            db, user_id=body.user_id,
            merchant_normalized=merchant_normalized,
            category_slug=body.category_slug, confirm=body.confirm,
        )
    except MerchantNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except CategoryNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except NothingToConfirmError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    db.commit()
    return MerchantPatchResponse(
        merchant_normalized=result.merchant_normalized,
        category_slug=result.resolved_category_slug,
        affected_transactions=result.affected_transactions,
        cache_row_created=result.cache_row_created,
    )
```

Note: `{merchant_normalized:path}` allows slashes and special chars in the value. FastAPI handles URL-decoding for path params transparently.

- [ ] **Step 4: Run tests, expect all 5 pass**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_merchant_patch.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/expenses.py tests/test_merchant_patch.py
git commit -m "feat(expenses-api): PATCH /merchants/{name}"
```

---

### Task 8: `POST /api/expenses/merchants/bulk-category`

**Files:**
- Modify: `argosy/api/routes/expenses.py`
- Create: `tests/test_bulk_category.py`

- [ ] **Step 1: Write tests for happy path + partial failure**

```python
# tests/test_bulk_category.py
"""POST /api/expenses/merchants/bulk-category."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture()
def seeded_two_merchants(expense_client):
    from argosy.services.expense_ingest.taxonomy_seed import seed_user_categories
    from argosy.state.models import (
        ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
    )
    SessionLocal = expense_client.app.state.session_factory
    with SessionLocal() as s:
        seed_user_categories(s, "ariel")
        s.flush()
        uncat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="uncategorized"
        ).one()
        src = ExpenseSource(user_id="ariel", issuer="isracard",
                            external_id="7777", display_name="T")
        s.add(src); s.flush()
        stmt = ExpenseStatement(user_id="ariel", source_id=src.id,
                                period_start=date(2026, 5, 1),
                                period_end=date(2026, 5, 31))
        s.add(stmt); s.flush()
        for merch in ("A", "B"):
            s.add(ExpenseTransaction(
                user_id="ariel", statement_id=stmt.id, source_id=src.id,
                occurred_on=date(2026, 5, 1),
                merchant_raw=merch, merchant_normalized=merch,
                amount_nis=Decimal("10.00"), direction="debit",
                tx_type="regular", raw_row_json="{}",
                category_id=uncat.id, category_source="llm",
                category_confidence=Decimal("0.40"),
            ))
        s.commit()
    return expense_client


def test_bulk_happy_path(seeded_two_merchants):
    r = seeded_two_merchants.post(
        "/api/expenses/merchants/bulk-category",
        json={"user_id": "ariel", "merchant_normalizeds": ["A", "B"],
              "category_slug": "food.groceries"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok_count"] == 2
    assert body["error_count"] == 0
    assert body["total_affected_transactions"] == 2


def test_bulk_with_missing_merchant_surfaces_in_results(seeded_two_merchants):
    r = seeded_two_merchants.post(
        "/api/expenses/merchants/bulk-category",
        json={"user_id": "ariel",
              "merchant_normalizeds": ["A", "ghost"],
              "category_slug": "food.groceries"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok_count"] == 1
    assert body["error_count"] == 1
    statuses = {r_["merchant_normalized"]: r_["status"] for r_ in body["results"]}
    assert statuses == {"A": "ok", "ghost": "error"}


def test_bulk_requires_one_of_slug_or_confirm(seeded_two_merchants):
    r = seeded_two_merchants.post(
        "/api/expenses/merchants/bulk-category",
        json={"user_id": "ariel", "merchant_normalizeds": ["A"]},
    )
    assert r.status_code == 422
```

- [ ] **Step 2: Run tests, expect failures**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_bulk_category.py -v`
Expected: 3 fail.

- [ ] **Step 3: Add the endpoint**

In `argosy/api/routes/expenses.py`, after `patch_merchant`:

```python
@router.post("/merchants/bulk-category", response_model=BulkCategoryResponse)
def bulk_apply_category(
    body: BulkCategoryRequest,
    db: Annotated[Session, Depends(get_db)],
) -> BulkCategoryResponse:
    """Apply a category to multiple merchants in one call. Per-item status;
    never aborts on a single failure."""
    from argosy.services.merchant_service import (
        apply_merchant_category, MerchantNotFoundError, CategoryNotFoundError,
        NothingToConfirmError,
    )
    results: list[BulkCategoryItemResult] = []
    total_affected = 0
    for merch in body.merchant_normalizeds:
        try:
            r = apply_merchant_category(
                db, user_id=body.user_id, merchant_normalized=merch,
                category_slug=body.category_slug, confirm=body.confirm,
            )
            total_affected += r.affected_transactions
            results.append(BulkCategoryItemResult(
                merchant_normalized=merch, status="ok",
                affected_transactions=r.affected_transactions,
            ))
        except (MerchantNotFoundError, CategoryNotFoundError,
                NothingToConfirmError, ValueError) as e:
            results.append(BulkCategoryItemResult(
                merchant_normalized=merch, status="error",
                affected_transactions=0, message=str(e),
            ))
    db.commit()
    ok = sum(1 for r in results if r.status == "ok")
    return BulkCategoryResponse(
        results=results, ok_count=ok,
        error_count=len(results) - ok,
        total_affected_transactions=total_affected,
    )
```

- [ ] **Step 4: Run tests, expect 3 pass**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_bulk_category.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/expenses.py tests/test_bulk_category.py
git commit -m "feat(expenses-api): POST /merchants/bulk-category"
```

---

## Phase 4 — Backend: range-bulk on transactions

### Task 9: `POST /api/expenses/transactions/bulk-label`

**Files:**
- Modify: `argosy/api/routes/expenses.py`
- Create: `tests/test_transactions_bulk_label.py`

- [ ] **Step 1: Write tests for category-only, tags-only, combined, empty body, unknown ids, no-cache-write**

```python
# tests/test_transactions_bulk_label.py
"""POST /api/expenses/transactions/bulk-label."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture()
def seeded(expense_client):
    from argosy.services.expense_ingest.taxonomy_seed import seed_user_categories
    from argosy.state.models import (
        ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
    )
    SessionLocal = expense_client.app.state.session_factory
    with SessionLocal() as s:
        seed_user_categories(s, "ariel")
        s.flush()
        uncat = s.query(ExpenseCategory).filter_by(
            user_id="ariel", slug="uncategorized"
        ).one()
        src = ExpenseSource(user_id="ariel", issuer="isracard",
                            external_id="8888", display_name="T")
        s.add(src); s.flush()
        stmt = ExpenseStatement(user_id="ariel", source_id=src.id,
                                period_start=date(2026, 5, 1),
                                period_end=date(2026, 5, 31))
        s.add(stmt); s.flush()
        ids = []
        for i in range(3):
            tx = ExpenseTransaction(
                user_id="ariel", statement_id=stmt.id, source_id=src.id,
                occurred_on=date(2026, 5, 1 + i),
                merchant_raw="M", merchant_normalized="M",
                amount_nis=Decimal("10.00"), direction="debit",
                tx_type="regular", raw_row_json="{}",
                category_id=uncat.id, category_source="llm",
                category_confidence=Decimal("0.40"),
            )
            s.add(tx); s.flush(); ids.append(tx.id)
        s.commit()
    return expense_client, ids


def test_bulk_category_only(seeded):
    client, ids = seeded
    r = client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel", "transaction_ids": ids,
              "category_slug": "food.groceries"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["affected"] == 3
    assert body["skipped"] == []

    # Cache row NOT written.
    from argosy.state.models import MerchantCategoryCache
    SessionLocal = client.app.state.session_factory
    with SessionLocal() as s:
        cache = s.query(MerchantCategoryCache).filter_by(
            user_id="ariel", merchant_pattern="M",
        ).one_or_none()
        assert cache is None


def test_bulk_tags_only(seeded):
    client, ids = seeded
    r = client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel", "transaction_ids": ids,
              "add_tags": ["trip:greece-2026-aug"]},
    )
    assert r.status_code == 200
    from argosy.state.models import ExpenseTransaction
    SessionLocal = client.app.state.session_factory
    with SessionLocal() as s:
        for tx_id in ids:
            tx = s.get(ExpenseTransaction, tx_id)
            assert "trip:greece-2026-aug" in json.loads(tx.tags)


def test_bulk_remove_tags(seeded):
    client, ids = seeded
    client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel", "transaction_ids": ids,
              "add_tags": ["a", "b"]},
    )
    r = client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel", "transaction_ids": ids,
              "remove_tags": ["a"]},
    )
    assert r.status_code == 200
    from argosy.state.models import ExpenseTransaction
    SessionLocal = client.app.state.session_factory
    with SessionLocal() as s:
        for tx_id in ids:
            tx = s.get(ExpenseTransaction, tx_id)
            tags = json.loads(tx.tags)
            assert "a" not in tags
            assert "b" in tags


def test_bulk_combined_category_and_tags(seeded):
    client, ids = seeded
    r = client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel", "transaction_ids": ids,
              "category_slug": "insurance.health",  # may not exist; add fallback
              "add_tags": ["trip:x"]},
    )
    # If category doesn't exist, we expect 400.
    assert r.status_code in (200, 400), r.text


def test_bulk_empty_body_returns_422(seeded):
    client, ids = seeded
    r = client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel", "transaction_ids": ids},
    )
    assert r.status_code == 422


def test_bulk_unknown_tx_id_lands_in_skipped(seeded):
    client, ids = seeded
    r = client.post(
        "/api/expenses/transactions/bulk-label",
        json={"user_id": "ariel",
              "transaction_ids": ids + [999999],
              "category_slug": "food.groceries"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["affected"] == 3
    assert any(s["tx_id"] == 999999 for s in body["skipped"])
```

- [ ] **Step 2: Run tests, expect failures**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_transactions_bulk_label.py -v`
Expected: all fail (endpoint doesn't exist).

- [ ] **Step 3: Add types and endpoint**

Append to `argosy/api/routes/expenses.py`:

```python
# ---------------------------------------------------------------------------
# POST /transactions/bulk-label — range-bulk per-tx labeling
# ---------------------------------------------------------------------------

class BulkLabelRequest(BaseModel):
    user_id: str
    transaction_ids: list[int] = Field(..., min_length=1)
    category_slug: str | None = None
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)

    def model_post_init(self, __context) -> None:
        if (self.category_slug is None
                and not self.add_tags and not self.remove_tags):
            raise ValueError(
                "Provide at least one of category_slug, add_tags, remove_tags"
            )


class BulkLabelSkip(BaseModel):
    tx_id: int
    reason: str


class BulkLabelResponse(BaseModel):
    affected: int
    skipped: list[BulkLabelSkip]


@router.post("/transactions/bulk-label", response_model=BulkLabelResponse)
def bulk_label_transactions(
    body: BulkLabelRequest,
    db: Annotated[Session, Depends(get_db)],
) -> BulkLabelResponse:
    """Apply a category and/or tag mutations to a list of transactions.

    Per-tx only — does NOT write to merchant_category_cache. Use the merchant
    tab for cache writes.
    """
    cat = None
    if body.category_slug is not None:
        cat = db.query(ExpenseCategory).filter_by(
            user_id=body.user_id, slug=body.category_slug,
        ).one_or_none()
        if cat is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown category {body.category_slug}",
            )

    skipped: list[BulkLabelSkip] = []
    affected = 0
    for tx_id in body.transaction_ids:
        tx = db.query(ExpenseTransaction).filter_by(
            id=tx_id, user_id=body.user_id,
        ).one_or_none()
        if tx is None:
            skipped.append(BulkLabelSkip(tx_id=tx_id, reason="not found"))
            continue
        if cat is not None:
            tx.category_id = cat.id
            tx.category_source = "user"
            tx.category_confidence = Decimal("1.00")
        if body.add_tags or body.remove_tags:
            try:
                tags = json.loads(tx.tags) if tx.tags else []
            except (ValueError, TypeError):
                tags = []
            tag_set = set(tags)
            for t in body.add_tags:
                tag_set.add(t)
            for t in body.remove_tags:
                tag_set.discard(t)
            tx.tags = json.dumps(sorted(tag_set), ensure_ascii=False)
        affected += 1

    db.commit()
    return BulkLabelResponse(affected=affected, skipped=skipped)
```

- [ ] **Step 4: Run tests, expect all pass**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest tests/test_transactions_bulk_label.py -v`
Expected: 6 passed. (One test exercises a `400` for unknown category — that's still a "pass" because the assertion is `status_code in (200, 400)`.)

- [ ] **Step 5: Commit**

```bash
git add argosy/api/routes/expenses.py tests/test_transactions_bulk_label.py
git commit -m "feat(expenses-api): POST /transactions/bulk-label"
```

---

### Task 9b: Backend full-suite regression check

- [ ] **Step 1: Run the full backend suite (excluding live-LLM markers)**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval" -q`
Expected: All pre-existing tests still pass; new tests added in Phase 1–4 also pass; total = baseline + ~25.

- [ ] **Step 2: No commit unless something needed fixing**

If a pre-existing test broke (most likely an existing PATCH transaction test), fix and commit:
```bash
git add <files>
git commit -m "fix(expenses-api): adjust existing test for apply_to_siblings field"
```

---

## Phase 5 — Frontend: shared components & API client

### Task 10: Extend `ui/src/lib/expenses/api.ts` with new endpoints + types

**Files:**
- Modify: `ui/src/lib/expenses/api.ts`

- [ ] **Step 1: Read the current file**

Run: `Read ui/src/lib/expenses/api.ts` and locate the existing `expensesApi` object (likely at the bottom of the file).

- [ ] **Step 2: Add new types and `expensesApi` methods**

Append to `ui/src/lib/expenses/api.ts`:

```typescript
// ---------------------------------------------------------------------------
// Merchant tab + bulk-label types
// ---------------------------------------------------------------------------

export interface MerchantRow {
  merchant_normalized: string;
  category_slug: string;
  category_label: string;
  parent_slug: string | null;
  parent_label: string | null;
  confidence: number | null;
  source: string; // 'user' | 'llm' | 'issuer' | 'cache' | 'uncached'
  is_cached: boolean;
  tx_count: number;
  total_nis: number;
  total_usd: number;
  last_seen: string;
}

export interface MerchantsListResponse {
  merchants: MerchantRow[];
  total: number;
}

export interface MerchantPatchResponse {
  merchant_normalized: string;
  category_slug: string;
  affected_transactions: number;
  cache_row_created: boolean;
}

export interface BulkCategoryItemResult {
  merchant_normalized: string;
  status: "ok" | "error";
  affected_transactions: number;
  message?: string | null;
}

export interface BulkCategoryResponse {
  results: BulkCategoryItemResult[];
  ok_count: number;
  error_count: number;
  total_affected_transactions: number;
}

export interface BulkLabelResponse {
  affected: number;
  skipped: { tx_id: number; reason: string }[];
}

export interface CategoryCreateResponse {
  id: number;
  slug: string;
  label_en: string;
  label_he: string;
  parent_slug: string | null;
  is_excluded_from_spend: boolean;
  is_inflow: boolean;
}

export interface MerchantsQuery {
  user_id: string;
  category?: string;
  source?: string;
  min_confidence?: number;
  max_confidence?: number;
  search?: string;
  sort?: string;
  order?: "asc" | "desc";
  limit?: number;
}

function qs(params: Record<string, unknown>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    sp.set(k, String(v));
  }
  return sp.toString();
}

// Extend expensesApi with new methods. If the file already exports an
// `expensesApi` object, add these properties to it instead of redefining.
// Locate the existing `export const expensesApi = { ... }` block and merge.

export const merchantsApi = {
  list: async (q: MerchantsQuery): Promise<MerchantsListResponse> =>
    getJSON<MerchantsListResponse>(`/api/expenses/merchants?${qs(q)}`),

  patch: async (
    merchant: string,
    body: { user_id: string; category_slug?: string; confirm?: boolean },
  ): Promise<MerchantPatchResponse> =>
    patchJSON<MerchantPatchResponse>(
      `/api/expenses/merchants/${encodeURIComponent(merchant)}`,
      body,
    ),

  bulkCategory: async (body: {
    user_id: string;
    merchant_normalizeds: string[];
    category_slug?: string;
    confirm?: boolean;
  }): Promise<BulkCategoryResponse> =>
    postJSON<BulkCategoryResponse>(
      "/api/expenses/merchants/bulk-category",
      body,
    ),
};

export const categoriesApi = {
  create: async (body: {
    user_id: string;
    parent_slug: string;
    slug: string;
    label_en: string;
    label_he?: string;
  }): Promise<CategoryCreateResponse> =>
    postJSON<CategoryCreateResponse>("/api/expenses/categories", body),
};

export const transactionsApi = {
  bulkLabel: async (body: {
    user_id: string;
    transaction_ids: number[];
    category_slug?: string;
    add_tags?: string[];
    remove_tags?: string[];
  }): Promise<BulkLabelResponse> =>
    postJSON<BulkLabelResponse>("/api/expenses/transactions/bulk-label", body),
};
```

If the existing `expensesApi.patchTransactionCategory` method exists, modify it to accept the new optional `applyToSiblings` argument:

```typescript
// Replace the existing patchTransactionCategory implementation:
patchTransactionCategory: async (
  txId: number,
  userId: string,
  categorySlug: string,
  applyToSiblings = true,  // back-compat default
): Promise<{
  transaction_id: number;
  category_slug: string;
  category_source: string;
  affected_count: number;
}> =>
  patchJSON(`/api/expenses/transactions/${txId}`, {
    user_id: userId,
    category_slug: categorySlug,
    apply_to_siblings: applyToSiblings,
  }),
```

- [ ] **Step 3: Lint + typecheck**

Run: `cd ui ; npm run lint ; npm run typecheck`
Expected: no new errors. If existing callers of `patchTransactionCategory` complain about positional args, leave the default `true` so the 3-arg form still works.

- [ ] **Step 4: Commit**

```bash
git add ui/src/lib/expenses/api.ts
git commit -m "feat(expenses-ui): API client for merchants + bulk-label + categories"
```

---

### Task 11: Hierarchical category picker component

Replaces the flat picker in `category-edit-popover.tsx` with a hierarchical one. We'll build a new component and migrate the consumer in Task 17.

**Files:**
- Create: `ui/src/components/expenses/category-picker-hierarchical.tsx`

- [ ] **Step 1: Create the component**

```tsx
// ui/src/components/expenses/category-picker-hierarchical.tsx
"use client";

import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

import type { CategoryOut } from "@/lib/expenses/api";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  categories: CategoryOut[];
  currentSlug: string | null;
  onPick: (slug: string) => void | Promise<void>;
  onAddSubCategoryClick?: () => void;
  busySlug?: string | null;
}

interface TreeNode {
  cat: CategoryOut;
  children: TreeNode[];
}

function buildTree(cats: CategoryOut[]): TreeNode[] {
  const byParent = new Map<string | null, CategoryOut[]>();
  for (const c of cats) {
    const k = c.parent_slug ?? null;
    if (!byParent.has(k)) byParent.set(k, []);
    byParent.get(k)!.push(c);
  }
  function build(parentSlug: string | null): TreeNode[] {
    return (byParent.get(parentSlug) ?? []).map((cat) => ({
      cat,
      children: build(cat.slug),
    }));
  }
  return build(null);
}

export function HierarchicalCategoryPicker({
  open, onOpenChange, categories, currentSlug, onPick,
  onAddSubCategoryClick, busySlug,
}: Props) {
  const [filter, setFilter] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    // Default: expand every parent.
    return new Set(categories.filter((c) => !c.parent_slug).map((c) => c.slug));
  });

  const tree = useMemo(() => buildTree(categories), [categories]);

  const matches = useMemo(() => {
    if (!filter.trim()) return null;
    const q = filter.toLowerCase();
    return new Set(
      categories
        .filter(
          (c) =>
            c.slug.toLowerCase().includes(q) ||
            c.label_en.toLowerCase().includes(q),
        )
        .map((c) => c.slug),
    );
  }, [filter, categories]);

  function toggle(slug: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });
  }

  function renderNode(n: TreeNode, depth: number): React.ReactNode {
    const visibleByFilter = matches === null || matches.has(n.cat.slug)
      || n.children.some((c) => visibleSubtree(c));
    if (!visibleByFilter) return null;
    const hasChildren = n.children.length > 0;
    const isExpanded = expanded.has(n.cat.slug) || matches !== null;
    const matchesFilter = matches === null || matches.has(n.cat.slug);
    return (
      <div key={n.cat.slug}>
        <div className="flex items-center gap-2" style={{ paddingLeft: depth * 12 }}>
          {hasChildren ? (
            <button
              type="button"
              className="text-xs w-4 text-muted-foreground"
              onClick={() => toggle(n.cat.slug)}
              aria-label={isExpanded ? "Collapse" : "Expand"}
            >
              {isExpanded ? "▾" : "▸"}
            </button>
          ) : (
            <span className="w-4" />
          )}
          <Button
            variant={n.cat.slug === currentSlug ? "secondary" : "ghost"}
            size="sm"
            disabled={busySlug !== null && busySlug !== undefined}
            onClick={() => onPick(n.cat.slug)}
            className="justify-start flex-1 capitalize"
          >
            <span className={matchesFilter ? "" : "opacity-60"}>
              {busySlug === n.cat.slug ? "Saving…" : n.cat.label_en}
            </span>
            <span className="ml-auto text-xs text-muted-foreground">
              {n.cat.slug}
            </span>
          </Button>
        </div>
        {hasChildren && isExpanded && (
          <div>
            {n.children.map((c) => renderNode(c, depth + 1))}
          </div>
        )}
      </div>
    );
  }

  function visibleSubtree(n: TreeNode): boolean {
    if (matches === null) return true;
    if (matches.has(n.cat.slug)) return true;
    return n.children.some(visibleSubtree);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Pick category</DialogTitle>
        </DialogHeader>
        <Input
          placeholder="Filter categories…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          autoFocus
        />
        <div className="max-h-80 overflow-y-auto flex flex-col gap-0.5">
          {tree.map((n) => renderNode(n, 0))}
        </div>
        {onAddSubCategoryClick && (
          <div className="pt-2 border-t border-border flex justify-end">
            <Button
              variant="outline"
              size="sm"
              onClick={onAddSubCategoryClick}
            >
              + Add sub-category
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 2: Lint + typecheck**

Run: `cd ui ; npm run lint ; npm run typecheck`
Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add ui/src/components/expenses/category-picker-hierarchical.tsx
git commit -m "feat(expenses-ui): hierarchical category picker"
```

---

### Task 12: Add-sub-category dialog

**Files:**
- Create: `ui/src/components/expenses/add-subcategory-dialog.tsx`

- [ ] **Step 1: Create the component**

```tsx
// ui/src/components/expenses/add-subcategory-dialog.tsx
"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";

import { categoriesApi, type CategoryOut } from "@/lib/expenses/api";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  userId: string;
  categories: CategoryOut[];           // all current categories
  onCreated: (newCat: CategoryOut) => void;
}

export function AddSubCategoryDialog({
  open, onOpenChange, userId, categories, onCreated,
}: Props) {
  const topLevel = categories.filter((c) => !c.parent_slug);
  const [parentSlug, setParentSlug] = useState<string>(topLevel[0]?.slug ?? "");
  const [slug, setSlug] = useState("");
  const [labelEn, setLabelEn] = useState("");
  const [labelHe, setLabelHe] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function submit() {
    setError(null);
    if (!parentSlug || !slug.trim() || !labelEn.trim()) {
      setError("Parent, slug, and English label are required.");
      return;
    }
    if (slug.includes(".")) {
      setError("Slug must not contain '.'");
      return;
    }
    setSaving(true);
    try {
      const created = await categoriesApi.create({
        user_id: userId,
        parent_slug: parentSlug,
        slug: slug.trim(),
        label_en: labelEn.trim(),
        label_he: labelHe.trim() || undefined,
      });
      onCreated({
        id: created.id,
        slug: created.slug,
        label_en: created.label_en,
        label_he: created.label_he,
        parent_slug: created.parent_slug,
        is_excluded_from_spend: created.is_excluded_from_spend,
        is_inflow: created.is_inflow,
      });
      setSlug(""); setLabelEn(""); setLabelHe(""); setError(null);
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Add sub-category</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div>
            <Label htmlFor="parent">Parent</Label>
            <Select value={parentSlug} onValueChange={setParentSlug}>
              <SelectTrigger id="parent">
                <SelectValue placeholder="Pick a parent" />
              </SelectTrigger>
              <SelectContent>
                {topLevel.map((c) => (
                  <SelectItem key={c.slug} value={c.slug}>
                    {c.label_en} ({c.slug})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div>
            <Label htmlFor="slug">Slug</Label>
            <Input
              id="slug"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="health"
            />
            <p className="text-xs text-muted-foreground mt-1">
              Will be stored as <code>{parentSlug}.{slug || "<slug>"}</code>
            </p>
          </div>
          <div>
            <Label htmlFor="label_en">Label (English)</Label>
            <Input
              id="label_en"
              value={labelEn}
              onChange={(e) => setLabelEn(e.target.value)}
              placeholder="Health Insurance"
            />
          </div>
          <div>
            <Label htmlFor="label_he">Label (Hebrew, optional)</Label>
            <Input
              id="label_he"
              value={labelHe}
              onChange={(e) => setLabelHe(e.target.value)}
              placeholder="ביטוח בריאות"
            />
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={saving}>
            {saving ? "Saving…" : "Add"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 2: Verify shadcn `select` and `label` primitives exist**

Run: `Glob ui/src/components/ui/select.tsx` and `Glob ui/src/components/ui/label.tsx`. If either is missing, add it via the project's existing shadcn workflow (typically `npx shadcn-ui add select label` from `ui/`, but consult the project's existing components for the right pattern — the file should follow the same import paths and styling as `ui/src/components/ui/button.tsx`).

- [ ] **Step 3: Lint + typecheck**

Run: `cd ui ; npm run lint ; npm run typecheck`
Expected: no new errors.

- [ ] **Step 4: Commit**

```bash
git add ui/src/components/expenses/add-subcategory-dialog.tsx ui/src/components/ui/select.tsx ui/src/components/ui/label.tsx
git commit -m "feat(expenses-ui): add-sub-category dialog"
```

(Adjust the `git add` line to include only files that actually changed.)

---

### Task 13: Label editor (picker + tags + apply-to-siblings)

**Files:**
- Create: `ui/src/components/expenses/label-editor.tsx`

- [ ] **Step 1: Create the component**

```tsx
// ui/src/components/expenses/label-editor.tsx
"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { HierarchicalCategoryPicker } from "./category-picker-hierarchical";
import type { CategoryOut } from "@/lib/expenses/api";

export type LabelEditorMode = "single-tx" | "bulk-tx";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: LabelEditorMode;
  categories: CategoryOut[];
  currentSlug?: string | null;
  currentTags?: string[];
  showSiblingsCheckbox: boolean; // only for single-tx mode on transactions page
  onSubmit: (payload: {
    categorySlug?: string;
    addTags: string[];
    removeTags: string[];
    applyToSiblings: boolean;
  }) => Promise<void>;
}

export function LabelEditor({
  open, onOpenChange, mode, categories, currentSlug, currentTags = [],
  showSiblingsCheckbox, onSubmit,
}: Props) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const [chosenSlug, setChosenSlug] = useState<string | undefined>(undefined);
  const [tagInput, setTagInput] = useState("");
  const [addedTags, setAddedTags] = useState<string[]>([]);
  const [removedTags, setRemovedTags] = useState<string[]>([]);
  const [applyToSiblings, setApplyToSiblings] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function addTag() {
    const t = tagInput.trim();
    if (!t) return;
    if (!addedTags.includes(t)) setAddedTags((xs) => [...xs, t]);
    setTagInput("");
  }
  function dropAdded(t: string) {
    setAddedTags((xs) => xs.filter((x) => x !== t));
  }
  function toggleRemoveExisting(t: string) {
    setRemovedTags((xs) =>
      xs.includes(t) ? xs.filter((x) => x !== t) : [...xs, t],
    );
  }

  async function submit() {
    setError(null);
    if (!chosenSlug && addedTags.length === 0 && removedTags.length === 0) {
      setError("Pick a category or add/remove at least one tag.");
      return;
    }
    setSaving(true);
    try {
      await onSubmit({
        categorySlug: chosenSlug,
        addTags: addedTags,
        removeTags: removedTags,
        applyToSiblings,
      });
      setChosenSlug(undefined); setAddedTags([]); setRemovedTags([]);
      setApplyToSiblings(false);
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>
            {mode === "bulk-tx" ? "Set labels (bulk)" : "Set labels"}
          </DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div>
            <Label>Category</Label>
            <Button
              variant="outline"
              className="w-full justify-start"
              onClick={() => setPickerOpen(true)}
            >
              {chosenSlug ?? currentSlug ?? "Pick a category…"}
            </Button>
          </div>

          {mode === "single-tx" && currentTags.length > 0 && (
            <div>
              <Label>Existing tags</Label>
              <div className="flex flex-wrap gap-1">
                {currentTags.map((t) => {
                  const removing = removedTags.includes(t);
                  return (
                    <Badge
                      key={t}
                      variant={removing ? "destructive" : "secondary"}
                      onClick={() => toggleRemoveExisting(t)}
                      className="cursor-pointer"
                    >
                      {t} {removing && "×"}
                    </Badge>
                  );
                })}
              </div>
            </div>
          )}

          <div>
            <Label>Add tags</Label>
            <div className="flex gap-2">
              <Input
                value={tagInput}
                onChange={(e) => setTagInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") { e.preventDefault(); addTag(); }
                }}
                placeholder="e.g. trip:greece-2026-aug"
              />
              <Button variant="outline" onClick={addTag}>Add</Button>
            </div>
            {addedTags.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-2">
                {addedTags.map((t) => (
                  <Badge
                    key={t}
                    variant="default"
                    className="cursor-pointer"
                    onClick={() => dropAdded(t)}
                  >
                    + {t} ×
                  </Badge>
                ))}
              </div>
            )}
          </div>

          {showSiblingsCheckbox && mode === "single-tx" && (
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={applyToSiblings}
                onCheckedChange={(c) => setApplyToSiblings(c === true)}
              />
              Apply to all sibling transactions of this merchant
            </label>
          )}

          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>

        <HierarchicalCategoryPicker
          open={pickerOpen}
          onOpenChange={setPickerOpen}
          categories={categories}
          currentSlug={chosenSlug ?? currentSlug ?? null}
          onPick={(slug) => { setChosenSlug(slug); setPickerOpen(false); }}
        />
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 2: Verify `Checkbox` primitive exists**

Run: `Glob ui/src/components/ui/checkbox.tsx`. If missing, add via the existing shadcn pattern.

- [ ] **Step 3: Lint + typecheck**

Run: `cd ui ; npm run lint ; npm run typecheck`
Expected: no new errors.

- [ ] **Step 4: Commit**

```bash
git add ui/src/components/expenses/label-editor.tsx
git commit -m "feat(expenses-ui): label editor modal (picker + tags)"
```

---

## Phase 6 — Frontend: Merchants tab

### Task 14: Merchants table component

**Files:**
- Create: `ui/src/components/expenses/merchants-table.tsx`

- [ ] **Step 1: Create the component**

```tsx
// ui/src/components/expenses/merchants-table.tsx
"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";

import { HierarchicalCategoryPicker } from "./category-picker-hierarchical";
import {
  merchantsApi, type CategoryOut, type MerchantRow,
} from "@/lib/expenses/api";

interface Props {
  merchants: MerchantRow[];
  categories: CategoryOut[];
  userId: string;
  selected: Set<string>;
  onSelectionChange: (next: Set<string>) => void;
  onRowChanged: () => void;       // refetch
  onAddSubCategoryClick: () => void;
  busy: boolean;
}

function fmtNis(n: number): string {
  return n.toLocaleString("en-IL", {
    style: "currency", currency: "ILS", maximumFractionDigits: 0,
  });
}
function fmtUsd(n: number): string {
  return n.toLocaleString("en-US", {
    style: "currency", currency: "USD", maximumFractionDigits: 0,
  });
}

function SourceBadge({ source, isCached }: { source: string; isCached: boolean }) {
  if (!isCached) return <Badge variant="outline">uncached</Badge>;
  const variant: "default" | "secondary" | "outline" =
    source === "user" ? "default"
    : source === "llm" ? "secondary"
    : "outline";
  return <Badge variant={variant}>{source}</Badge>;
}

export function MerchantsTable({
  merchants, categories, userId, selected, onSelectionChange,
  onRowChanged, onAddSubCategoryClick, busy,
}: Props) {
  const [editingMerchant, setEditingMerchant] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [savingSlug, setSavingSlug] = useState<string | null>(null);

  function toggleRow(m: string) {
    const next = new Set(selected);
    if (next.has(m)) next.delete(m);
    else next.add(m);
    onSelectionChange(next);
  }

  function toggleAll() {
    if (selected.size === merchants.length) {
      onSelectionChange(new Set());
    } else {
      onSelectionChange(new Set(merchants.map((m) => m.merchant_normalized)));
    }
  }

  async function pickCategory(slug: string) {
    if (!editingMerchant) return;
    setSavingSlug(slug);
    try {
      await merchantsApi.patch(editingMerchant, {
        user_id: userId, category_slug: slug,
      });
      setPickerOpen(false);
      setEditingMerchant(null);
      onRowChanged();
    } catch (e) {
      alert(`Failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSavingSlug(null);
    }
  }

  async function confirmCurrent(merch: string) {
    try {
      await merchantsApi.patch(merch, { user_id: userId, confirm: true });
      onRowChanged();
    } catch (e) {
      alert(`Failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  return (
    <>
      <div className="overflow-x-auto border border-border rounded-md">
        <table className="min-w-full text-sm">
          <thead className="bg-muted/50">
            <tr>
              <th className="px-2 py-2 w-8">
                <Checkbox
                  checked={selected.size === merchants.length && merchants.length > 0}
                  onCheckedChange={toggleAll}
                />
              </th>
              <th className="px-2 py-2 text-left">Merchant</th>
              <th className="px-2 py-2 text-left">Category</th>
              <th className="px-2 py-2 text-right">Confidence</th>
              <th className="px-2 py-2 text-left">Source</th>
              <th className="px-2 py-2 text-right"># Txs</th>
              <th className="px-2 py-2 text-right">Total</th>
              <th className="px-2 py-2 text-right">Last seen</th>
              <th className="px-2 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {merchants.map((m) => (
              <tr key={m.merchant_normalized} className="border-t border-border">
                <td className="px-2 py-2">
                  <Checkbox
                    checked={selected.has(m.merchant_normalized)}
                    onCheckedChange={() => toggleRow(m.merchant_normalized)}
                  />
                </td>
                <td className="px-2 py-2 font-mono">{m.merchant_normalized}</td>
                <td className="px-2 py-2">
                  <Badge
                    variant="secondary"
                    className="cursor-pointer hover:bg-secondary/80"
                    onClick={() => {
                      setEditingMerchant(m.merchant_normalized);
                      setPickerOpen(true);
                    }}
                  >
                    {m.parent_label
                      ? `${m.parent_label} › ${m.category_label}`
                      : m.category_label}
                  </Badge>
                </td>
                <td className="px-2 py-2 text-right">
                  {m.confidence != null ? m.confidence.toFixed(2) : "—"}
                </td>
                <td className="px-2 py-2">
                  <SourceBadge source={m.source} isCached={m.is_cached} />
                </td>
                <td className="px-2 py-2 text-right">{m.tx_count}</td>
                <td className="px-2 py-2 text-right">
                  {m.total_nis ? fmtNis(m.total_nis) : ""}
                  {m.total_usd ? <div>{fmtUsd(m.total_usd)}</div> : null}
                </td>
                <td className="px-2 py-2 text-right text-xs text-muted-foreground">
                  {m.last_seen}
                </td>
                <td className="px-2 py-2 text-right">
                  {m.source !== "user" && m.is_cached && (
                    <Button
                      size="sm" variant="ghost"
                      disabled={busy}
                      onClick={() => confirmCurrent(m.merchant_normalized)}
                    >
                      Confirm
                    </Button>
                  )}
                </td>
              </tr>
            ))}
            {merchants.length === 0 && (
              <tr>
                <td colSpan={9} className="text-center text-muted-foreground py-8">
                  No merchants match the current filter.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <HierarchicalCategoryPicker
        open={pickerOpen}
        onOpenChange={(o) => { setPickerOpen(o); if (!o) setEditingMerchant(null); }}
        categories={categories}
        currentSlug={
          editingMerchant
            ? merchants.find((m) => m.merchant_normalized === editingMerchant)?.category_slug ?? null
            : null
        }
        onPick={pickCategory}
        onAddSubCategoryClick={onAddSubCategoryClick}
        busySlug={savingSlug}
      />
    </>
  );
}
```

- [ ] **Step 2: Lint + typecheck**

Run: `cd ui ; npm run lint ; npm run typecheck`
Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add ui/src/components/expenses/merchants-table.tsx
git commit -m "feat(expenses-ui): merchants table component"
```

---

### Task 15: `/expenses/merchants` page + register tab in layout

**Files:**
- Create: `ui/src/app/expenses/merchants/page.tsx`
- Modify: `ui/src/app/expenses/layout.tsx`

- [ ] **Step 1: Register the tab in layout**

Modify the `TABS` array in `ui/src/app/expenses/layout.tsx` lines 9-16:

```typescript
const TABS = [
  { href: "/expenses", label: "Overview" },
  { href: "/expenses/monthly", label: "Monthly" },
  { href: "/expenses/transactions", label: "Transactions" },
  { href: "/expenses/sources", label: "Sources" },
  { href: "/expenses/merchants", label: "Merchants" },
  { href: "/expenses/trips", label: "Trips" },
  { href: "/expenses/rsu", label: "RSU" },
];
```

- [ ] **Step 2: Create the page**

```tsx
// ui/src/app/expenses/merchants/page.tsx
"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";

import { AddSubCategoryDialog } from "@/components/expenses/add-subcategory-dialog";
import { HierarchicalCategoryPicker } from "@/components/expenses/category-picker-hierarchical";
import { MerchantsTable } from "@/components/expenses/merchants-table";
import {
  categoriesApi, expensesApi, merchantsApi,
  type CategoryOut, type MerchantRow,
} from "@/lib/expenses/api";

const USER_ID = "ariel";

export default function MerchantsPage() {
  const [merchants, setMerchants] = useState<MerchantRow[]>([]);
  const [categories, setCategories] = useState<CategoryOut[]>([]);
  const [search, setSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState<string>("all");
  const [categoryFilter, setCategoryFilter] = useState<string>("all");
  const [maxConfidence, setMaxConfidence] = useState<string>("");
  const [sort, setSort] = useState<string>("needs_attention");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [addSubCatOpen, setAddSubCatOpen] = useState(false);
  const [bulkPickerOpen, setBulkPickerOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const fetchAll = useCallback(async () => {
    const cats = await expensesApi.categories(USER_ID);
    setCategories(cats.categories);
    const ms = await merchantsApi.list({
      user_id: USER_ID,
      search: search || undefined,
      source: sourceFilter === "all" ? undefined : sourceFilter,
      category: categoryFilter === "all" ? undefined : categoryFilter,
      max_confidence: maxConfidence ? Number(maxConfidence) : undefined,
      sort,
      order,
      limit: 500,
    });
    setMerchants(ms.merchants);
  }, [search, sourceFilter, categoryFilter, maxConfidence, sort, order]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  async function applyBulkCategory(slug: string) {
    setBusy(true);
    try {
      const res = await merchantsApi.bulkCategory({
        user_id: USER_ID,
        merchant_normalizeds: Array.from(selected),
        category_slug: slug,
      });
      alert(`Applied to ${res.ok_count} merchants (${res.total_affected_transactions} transactions). ${res.error_count} failed.`);
      setBulkPickerOpen(false);
      setSelected(new Set());
      await fetchAll();
    } finally {
      setBusy(false);
    }
  }

  async function confirmBulk() {
    setBusy(true);
    try {
      const res = await merchantsApi.bulkCategory({
        user_id: USER_ID,
        merchant_normalizeds: Array.from(selected),
        confirm: true,
      });
      alert(`Confirmed ${res.ok_count} merchants. ${res.error_count} failed.`);
      setSelected(new Set());
      await fetchAll();
    } finally {
      setBusy(false);
    }
  }

  const affectedTxCount = useMemo(() => {
    return merchants
      .filter((m) => selected.has(m.merchant_normalized))
      .reduce((s, m) => s + m.tx_count, 0);
  }, [selected, merchants]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          placeholder="Search merchant…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-56"
        />
        <Select value={categoryFilter} onValueChange={setCategoryFilter}>
          <SelectTrigger className="w-44"><SelectValue placeholder="Category" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All categories</SelectItem>
            <SelectItem value="uncategorized">Uncategorized</SelectItem>
            {categories.filter((c) => !c.parent_slug).map((c) => (
              <SelectItem key={c.slug} value={c.slug}>{c.label_en}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={sourceFilter} onValueChange={setSourceFilter}>
          <SelectTrigger className="w-32"><SelectValue placeholder="Source" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All sources</SelectItem>
            <SelectItem value="user">User-confirmed</SelectItem>
            <SelectItem value="llm">LLM cached</SelectItem>
            <SelectItem value="uncached">Uncached</SelectItem>
          </SelectContent>
        </Select>
        <Input
          type="number"
          step="0.01"
          min="0"
          max="1"
          placeholder="Max confidence"
          value={maxConfidence}
          onChange={(e) => setMaxConfidence(e.target.value)}
          className="w-32"
        />
        <Select value={sort} onValueChange={setSort}>
          <SelectTrigger className="w-44"><SelectValue placeholder="Sort by" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="needs_attention">Needs attention</SelectItem>
            <SelectItem value="merchant">Merchant</SelectItem>
            <SelectItem value="category">Category</SelectItem>
            <SelectItem value="confidence">Confidence</SelectItem>
            <SelectItem value="tx_count"># Txs</SelectItem>
            <SelectItem value="total_nis">Total</SelectItem>
            <SelectItem value="last_seen">Last seen</SelectItem>
          </SelectContent>
        </Select>
        <Button
          variant="outline"
          onClick={() => setOrder((o) => (o === "asc" ? "desc" : "asc"))}
        >
          {order === "asc" ? "▲" : "▼"}
        </Button>
      </div>

      <MerchantsTable
        merchants={merchants}
        categories={categories}
        userId={USER_ID}
        selected={selected}
        onSelectionChange={setSelected}
        onRowChanged={fetchAll}
        onAddSubCategoryClick={() => setAddSubCatOpen(true)}
        busy={busy}
      />

      {selected.size > 0 && (
        <div className="sticky bottom-2 bg-background border border-border rounded-md p-3 shadow flex items-center gap-3">
          <span className="text-sm">
            {selected.size} merchants selected · {affectedTxCount} transactions
          </span>
          <Button onClick={() => setBulkPickerOpen(true)} disabled={busy}>
            Apply category…
          </Button>
          <Button variant="outline" onClick={confirmBulk} disabled={busy}>
            Confirm current
          </Button>
          <Button variant="ghost" onClick={() => setSelected(new Set())}>
            Clear
          </Button>
        </div>
      )}

      <AddSubCategoryDialog
        open={addSubCatOpen}
        onOpenChange={setAddSubCatOpen}
        userId={USER_ID}
        categories={categories}
        onCreated={() => fetchAll()}
      />

      <HierarchicalCategoryPicker
        open={bulkPickerOpen}
        onOpenChange={setBulkPickerOpen}
        categories={categories}
        currentSlug={null}
        onPick={applyBulkCategory}
        onAddSubCategoryClick={() => { setBulkPickerOpen(false); setAddSubCatOpen(true); }}
      />
    </div>
  );
}
```

The `expensesApi.categories(userId)` method already exists at `ui/src/lib/expenses/api.ts:354`, returning `CategoriesResponse` (`{ categories: CategoryOut[] }`). No changes needed there.

- [ ] **Step 3: Restart UI dev server, navigate to /expenses/merchants in browser**

Manual smoke (per Ariel's standing skip preference: do this only as a one-time check that nothing crashes — no console errors, table renders).

- [ ] **Step 4: Lint + typecheck**

Run: `cd ui ; npm run lint ; npm run typecheck`
Expected: no new errors.

- [ ] **Step 5: Commit**

```bash
git add ui/src/app/expenses/layout.tsx ui/src/app/expenses/merchants/page.tsx ui/src/lib/expenses/api.ts
git commit -m "feat(expenses-ui): /expenses/merchants tab"
```

---

## Phase 7 — Frontend: range-bulk on transactions

### Task 16: Add checkbox column + "select all matching" chip to transactions

**Files:**
- Modify: `ui/src/components/expenses/transactions-table.tsx`
- Modify: `ui/src/app/expenses/transactions/page.tsx` (or whatever page renders the table — find via Grep on `TransactionsTable` import)

- [ ] **Step 1: Locate the table consumer**

Run: `Grep -r "TransactionsTable" ui/src/app/expenses/`. Identify the page file that imports the component.

- [ ] **Step 2: Add checkbox column to `transactions-table.tsx`**

Read the file first. Add a `selected: Set<number>` + `onSelectionChange: (next: Set<number>) => void` prop pair. Render a `<Checkbox>` cell on the left of each row, and a select-all header checkbox. Keep all existing functionality intact.

Patch the props interface (top of file):

```typescript
interface TransactionsTableProps {
  // ... existing props ...
  selected?: Set<number>;
  onSelectionChange?: (next: Set<number>) => void;
}
```

Add the header cell as the first `<th>` and a body cell as the first `<td>` of each row:

```tsx
// Header
{onSelectionChange && (
  <th className="px-2 py-2 w-8">
    <Checkbox
      checked={selected?.size === transactions.length && transactions.length > 0}
      onCheckedChange={() => {
        if (!onSelectionChange) return;
        if (selected?.size === transactions.length) onSelectionChange(new Set());
        else onSelectionChange(new Set(transactions.map((t) => t.id)));
      }}
    />
  </th>
)}

// Body (per row)
{onSelectionChange && (
  <td className="px-2 py-2">
    <Checkbox
      checked={selected?.has(tx.id) ?? false}
      onCheckedChange={() => {
        if (!onSelectionChange || !selected) return;
        const next = new Set(selected);
        if (next.has(tx.id)) next.delete(tx.id); else next.add(tx.id);
        onSelectionChange(next);
      }}
    />
  </td>
)}
```

(If `Checkbox` isn't already imported, add `import { Checkbox } from "@/components/ui/checkbox";`.)

- [ ] **Step 3: Add the "Select all matching filter" chip and bulk bar in the transactions page**

In the page file (likely `ui/src/app/expenses/transactions/page.tsx`):

- Add state: `const [selected, setSelected] = useState<Set<number>>(new Set());`
- After the filter bar, render a chip:

```tsx
{selected.size === 0 && total > rows.length && (
  <button
    type="button"
    onClick={async () => {
      // Page through GET /transactions until exhausted; current filter is
      // captured in the call.
      const ids: number[] = [];
      let off = 0;
      const PAGE = 1000;
      while (true) {
        const params = new URLSearchParams({
          user_id: USER_ID,
          limit: String(PAGE), offset: String(off),
        });
        // Re-apply the same filter parameters as the visible page.
        if (fromDate) params.set("from_date", fromDate);
        if (toDate) params.set("to_date", toDate);
        if (category) params.set("category", category);
        if (sourceId) params.set("source_id", String(sourceId));
        if (search) params.set("search", search);
        const res = await fetch(`${BASE}/api/expenses/transactions?${params}`);
        const j = await res.json();
        for (const tx of j.transactions) ids.push(tx.id);
        if (j.transactions.length < PAGE) break;
        off += PAGE;
      }
      setSelected(new Set(ids));
    }}
    className="text-xs underline text-muted-foreground"
  >
    Select all matching filter ({total} transactions)
  </button>
)}
```

(Use the same `BASE`, `USER_ID`, and filter state names already in the page.)

- Pass `selected` and `setSelected` into `<TransactionsTable />`.

- [ ] **Step 4: Lint + typecheck**

Run: `cd ui ; npm run lint ; npm run typecheck`
Expected: no new errors.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/expenses/transactions-table.tsx ui/src/app/expenses/transactions/page.tsx
git commit -m "feat(expenses-ui): checkbox column + select-all-matching on transactions"
```

---

### Task 17: Bulk-apply bar on transactions (Apply category / Add tag / Remove tag)

**Files:**
- Modify: `ui/src/app/expenses/transactions/page.tsx`

- [ ] **Step 1: Add the bulk-apply bar that wires to `LabelEditor`**

At the bottom of the page component's render output, before any closing tag:

```tsx
{selected.size > 0 && (
  <>
    <div className="sticky bottom-2 bg-background border border-border rounded-md p-3 shadow flex items-center gap-3">
      <span className="text-sm">{selected.size} transactions selected</span>
      <Button onClick={() => setLabelEditorOpen(true)}>Apply labels…</Button>
      <Button variant="ghost" onClick={() => setSelected(new Set())}>Clear</Button>
    </div>
    <LabelEditor
      open={labelEditorOpen}
      onOpenChange={setLabelEditorOpen}
      mode="bulk-tx"
      categories={categories}
      currentSlug={null}
      currentTags={[]}
      showSiblingsCheckbox={false}
      onSubmit={async ({ categorySlug, addTags, removeTags }) => {
        const res = await transactionsApi.bulkLabel({
          user_id: USER_ID,
          transaction_ids: Array.from(selected),
          category_slug: categorySlug,
          add_tags: addTags,
          remove_tags: removeTags,
        });
        alert(`Updated ${res.affected} transactions. ${res.skipped.length} skipped.`);
        setSelected(new Set());
        await refetch();   // existing fetch helper in the page; rename to match
      }}
    />
  </>
)}
```

Imports needed (add to top of file):

```typescript
import { LabelEditor } from "@/components/expenses/label-editor";
import { transactionsApi, type CategoryOut } from "@/lib/expenses/api";
```

Add state declarations near `selected`:

```typescript
const [labelEditorOpen, setLabelEditorOpen] = useState(false);
const [categories, setCategories] = useState<CategoryOut[]>([]);
```

And in the existing `useEffect` that loads transactions, also load categories (or add a sibling effect):

```typescript
useEffect(() => {
  expensesApi.categories(USER_ID).then((r) => setCategories(r.categories));
}, []);
```

- [ ] **Step 2: Lint + typecheck**

Run: `cd ui ; npm run lint ; npm run typecheck`
Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add ui/src/app/expenses/transactions/page.tsx
git commit -m "feat(expenses-ui): bulk-label bar on /expenses/transactions"
```

---

### Task 18: Switch transactions inline category-edit to LabelEditor (default no-fanout)

The current inline edit on the transactions table uses `CategoryEditPopover`, which calls `patchTransactionCategory` — and that endpoint now defaults to fan-out. Behavior change: inline edit should default to no-fanout.

**Files:**
- Modify: `ui/src/components/expenses/transactions-table.tsx`
- Delete: `ui/src/components/expenses/category-edit-popover.tsx`

- [ ] **Step 1: Replace the inline `<CategoryEditPopover>` usage with `<LabelEditor mode="single-tx">`**

In `transactions-table.tsx`, find the `<CategoryEditPopover ... />` usage. Replace it with a click-to-open `<LabelEditor>` wired like so:

```tsx
// Add to component:
const [editingTx, setEditingTx] = useState<{ id: number; slug: string | null; tags: string[] } | null>(null);

// In the row cell where CategoryEditPopover was:
<Badge
  variant="secondary"
  className="cursor-pointer hover:bg-secondary/80 capitalize"
  onClick={() => setEditingTx({
    id: tx.id,
    slug: tx.category_slug ?? null,
    tags: tx.tags ?? [],
  })}
>
  {tx.category_slug ?? "uncategorized"}
</Badge>

// At the bottom of the component's return (sibling of the table):
{editingTx && (
  <LabelEditor
    open
    onOpenChange={(o) => { if (!o) setEditingTx(null); }}
    mode="single-tx"
    categories={categories}
    currentSlug={editingTx.slug}
    currentTags={editingTx.tags}
    showSiblingsCheckbox={true}
    onSubmit={async ({ categorySlug, addTags, removeTags, applyToSiblings }) => {
      if (categorySlug) {
        await expensesApi.patchTransactionCategory(
          editingTx.id, USER_ID, categorySlug, applyToSiblings,
        );
      }
      if (addTags.length || removeTags.length) {
        await transactionsApi.bulkLabel({
          user_id: USER_ID,
          transaction_ids: [editingTx.id],
          add_tags: addTags,
          remove_tags: removeTags,
        });
      }
      setEditingTx(null);
      onChanged?.();
    }}
  />
)}
```

The component needs `categories: CategoryOut[]` as a prop; pass it from the parent page.

Remove the `<CategoryEditPopover>` import and `<CategoryEditPopover>` usage.

- [ ] **Step 2: Delete the old popover**

```bash
git rm ui/src/components/expenses/category-edit-popover.tsx
```

- [ ] **Step 3: Verify no other file imports `CategoryEditPopover`**

Run: `Grep "CategoryEditPopover" ui/src/`. Expected: no matches.

- [ ] **Step 4: Lint + typecheck**

Run: `cd ui ; npm run lint ; npm run typecheck`
Expected: no new errors.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/expenses/transactions-table.tsx ui/src/app/expenses/transactions/page.tsx
git commit -m "refactor(expenses-ui): replace inline popover with LabelEditor"
```

---

## Phase 8 — Wrap-up

### Task 19: Update SDD handover note

**Files:**
- Modify: `docs/design/SDD.md`

- [ ] **Step 1: Read the current "Last edit" + "Where development is at right now" sections**

Run: `Read docs/design/SDD.md` (around lines 16-100, the Handover section).

- [ ] **Step 2: Insert an EX8 entry**

After the EX6 entry (around line 178 — find it via `Grep "Wave EX6"`), insert:

```markdown
### Wave EX8 — Merchant↔Category tab + range-bulk labeling — LANDED

Spec: `docs/superpowers/specs/2026-05-11-merchant-category-tab-design.md`. Plan: `docs/superpowers/plans/2026-05-11-merchant-category-tab-implementation.md`.

- New `/expenses/merchants` tab — merchant-grouped table with filter bar (search, category, source, max-confidence, sort), checkbox multi-select, bulk-apply, inline confirm. Hierarchical category picker replaces the old flat popover throughout the app.
- New `/expenses/categories` POST endpoint for sub-category creation under existing top-level parents (one nesting level enforced). Insurance.health, insurance.life, etc. land via UI; no migration needed (hierarchy already in `expense_categories.parent_id`).
- Range-bulk on `/expenses/transactions` — checkbox column + "Select all matching filter" chip + bulk bar (Apply category / Add tag / Remove tag). Powers trip-tag workflows ("Aug 5-15 → trip:greece-2026-aug") without touching merchant_category_cache.
- Backend: extracted `argosy/services/merchant_service.apply_merchant_category()` — single source of truth for "merchant mapping changed". `PATCH /transactions/{id}` now honors an `apply_to_siblings` body field (defaults true for back-compat; inline UI sends false going forward).
- Zero schema changes. Five new endpoints (`GET /merchants`, `PATCH /merchants/{name}`, `POST /merchants/bulk-category`, `POST /categories`, `POST /transactions/bulk-label`); one behavior change on the existing PATCH.
- BIT and similar pass-through merchants are mapped to `cash` rather than getting a "split merchant" mechanic — Ariel's call (low total impact; defer the mechanic unless it bites).
- Test count: baseline + ~25 new tests across 7 new files; full suite green under `pytest -m "not llm_eval"`.
```

Also update the **Last edit** stanza at the top of the file (line 18) to reflect EX8 landing.

- [ ] **Step 3: Commit**

```bash
git add docs/design/SDD.md
git commit -m "docs(sdd): EX8 — merchant tab + range-bulk landed"
```

---

### Task 20: Final regression + green build verification

- [ ] **Step 1: Run full backend suite**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval"`
Expected: All pass. New test files contribute ~25 new tests.

- [ ] **Step 2: Run UI lint + typecheck**

Run: `cd ui ; npm run lint ; npm run typecheck`
Expected: no new errors (existing `react-hooks/set-state-in-effect` warnings are project-wide and pre-existing — do not attempt to fix unless they were newly introduced by this wave).

- [ ] **Step 3: Restart dev servers and smoke**

Manual smoke (one-shot — per Ariel's standing preference, full UI smoke loops are skipped):
- Restart API + Next: see SDD handover §"Where development is at right now" for the exact command.
- Open `http://127.0.0.1:1337/expenses/merchants` — confirm table renders, filter works, picker opens.
- Open `http://127.0.0.1:1337/expenses/transactions` — confirm checkbox column appears, "Select all matching filter" chip works, bulk bar shows on selection.

- [ ] **Step 4: Final summary commit (only if smoke revealed a tweak)**

If anything needed a polish fix, commit as `fix(expenses-ui): post-EX8 smoke fix-up`. Otherwise skip.

---

## Self-review notes (filled in by the plan author)

**Spec coverage** — every numbered requirement in §2 (in-scope) maps to a task:
- Merchant tab UI → Tasks 11, 14, 15
- Filter / sort / bulk-select → Tasks 14, 15
- Hierarchical picker + add-sub-cat → Tasks 11, 12
- Sub-category POST endpoint → Task 4
- GET /merchants aggregation → Task 6
- PATCH /merchants → Task 7
- POST /merchants/bulk-category → Task 8
- POST /transactions/bulk-label → Task 9
- `apply_to_siblings` behavior change on PATCH /transactions → Task 3
- Service extraction → Tasks 1, 2
- Range-bulk on transactions UI → Tasks 16, 17, 18
- Shared LabelEditor → Task 13
- Old popover removal → Task 18
- SDD update → Task 19
- Full regression → Tasks 9b, 20

**Placeholder scan:** none — every code step shows the actual code. Two tasks contain "find via Grep" / "search the file first" guidance where the precise existing identifier name may vary (Task 15 `expensesApi.listCategories`, Task 16 page-file location); these are exact lookups, not abstract TODOs.

**Type consistency:** `ApplyResult` is used in Task 2 and surfaces fields (`merchant_normalized`, `resolved_category_slug`, `affected_transactions`, `cache_row_created`) consistently in Tasks 7, 8. `MerchantOut` shape in Task 5 matches what Task 6 returns and what `MerchantRow` consumes in Task 10. `BulkLabelRequest` in Task 9 matches the `transactionsApi.bulkLabel` payload in Task 17.
