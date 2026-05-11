# Merchant ↔ Category Tab + Range-Bulk Labeling

| Field | Value |
|---|---|
| **Wave** | EX8 (categorization ergonomics) |
| **Date** | 2026-05-11 |
| **Status** | Spec — drafted from brainstorm dialog with Ariel; awaiting user review |
| **Authors** | Claude + Ariel |
| **Predecessor waves** | EX1 (categorizer + cache, landed), EX1.1 (stabilization, landed), EX4–EX6 (dashboard surfaces, landed) |
| **Sibling spec** | None — this wave is standalone |

---

## 1. Purpose

The categorizer agent assigns a category to every transaction at ingest time and caches merchant→category rules in `merchant_category_cache` with a `confidence` score and a `source` (`llm` / `issuer` / `user`). Today there is no UI surface for managing these mappings as a group. Per-row category edits exist on `/expenses/transactions` via the inline `CategoryEditPopover`, but if you want to "see every merchant whose mapping is below 0.95 confidence and confirm or fix them in one pass", you have to read the transactions table row-by-row.

This wave introduces:

- **`/expenses/merchants` tab** — a merchant-grouped, filterable, sortable view of every `merchant_normalized → category` mapping with confidence, source, tx count, and total spend. Multi-select + bulk-apply for "fix everything quickly". Hierarchical category picker. An "add sub-category" path for filling taxonomy gaps (e.g. `insurance.health` under existing `insurance`).
- **Date-range bulk-label on `/expenses/transactions`** — select all transactions matching a date filter, apply a category and/or add/remove tags in one POST. Per-tx only (does not write to `merchant_category_cache`), supporting trip-tagging workflows where the same merchant (e.g. a travel insurer) might belong to different trips on different dates.

## 2. Scope

### In scope

- New tab `/expenses/merchants` with merchant-aggregated table, filter bar, bulk-select.
- One-level sub-category creation under existing top-level categories (e.g. add `insurance.health` if `insurance` already exists). No rename, no delete.
- Hierarchical category picker reused in three places: merchant edit, transaction inline edit, bulk-apply modal.
- Date-range bulk-label flow on `/expenses/transactions` — adds a "select all matching filter" affordance + a bulk-apply bar.
- One backward-compatible behavior change on `PATCH /api/expenses/transactions/{id}`: explicit `apply_to_siblings` flag (default `false` from the inline-edit UI, default `true` from the merchant tab). The current implicit fan-out becomes opt-in.
- Backend service extraction: a single `apply_merchant_category(...)` helper that backs both the existing transaction PATCH (in fan-out mode) and the new merchant PATCH.
- Backend tests for every new endpoint and the service extraction.

### Out of scope

- **No schema changes / no migrations.** All needed columns already exist (`merchant_category_cache.confidence`, `.source`, `.hit_count`; `expense_categories.parent_id`; `expense_transactions.tags`).
- **No "split merchant" mechanic.** BIT and similar pass-through merchants are mapped to `cash` (a single category) — Ariel's call. If a BIT transaction is actually a vet payment or a trip expense, the user re-tags that row individually via inline edit or range-bulk.
- **No top-level category creation.** Only sub-categories under existing parents.
- **No multi-level nesting.** MVP enforces one level (`insurance.health` allowed, `insurance.health.dental` rejected even though the DB supports it).
- **No category rename / delete.** Defer to a future wave if the taxonomy needs remodeling.
- **No regex pattern editor.** `merchant_category_cache.is_regex` stays a seed-time concern.
- **No merchant renaming.** `merchant_normalized` is what it is.
- **No bulk-label-by-filter endpoint.** Range-bulk passes explicit `transaction_ids` for safety; UI is responsible for resolving "all matching filter" into the ID list before POSTing.
- **No unification of the `category_id` FK and the `tags` JSON array in the DB.** They remain separate fields with separate semantics. Only the labeling UI is unified.
- **No mobile-responsive audit** beyond Tailwind defaults.
- **No frontend unit tests / Playwright.** Per Ariel's standing preference, manual UI smokes are skipped; lint + typecheck must stay clean.

## 3. Background

### What's available right now

Backend (`argosy/state/models.py`, `argosy/services/expense_categorize/`, `argosy/api/routers/expenses.py`):

- `merchant_category_cache(id, user_id, merchant_pattern, is_regex, category_id, source, confidence, hit_count, last_hit_at, created_at)` — keyed `UNIQUE(user_id, merchant_pattern, is_regex)`. `source ∈ {user, llm, issuer}`. `confidence` is `Decimal(3,2)`. LLM verdicts <0.85 are not cached; user overrides always confidence=1.00.
- `expense_categories(id, user_id, slug, label_en, label_he, parent_id, is_excluded_from_spend, is_inflow, display_order)` — supports hierarchy via `parent_id`. System defaults (`user_id IS NULL`) are seeded into per-user copies on first ingest via `seed_user_categories()`.
- `expense_transactions.category_id` (FK), `.category_source` (string), `.category_confidence` (Decimal), `.tags` (JSON array of strings) — already populated by the categorizer.
- `category_resolver.py` — precedence: cache → issuer hint → LLM batch. Threshold ≥0.85 to cache LLM verdicts.
- `GET /api/expenses/categories?user_id=` — returns flat list including `parent_slug`. Hierarchy is reconstructable client-side.
- `GET /api/expenses/transactions?user_id=&...filters` — returns `TransactionOut` with `category_slug`, `category_source`.
- `PATCH /api/expenses/transactions/{transaction_id}` — overrides a tx's category and currently auto-fans-out to every tx with the same `merchant_normalized`. Writes a cache row with `source=user`, `confidence=1.00`. Returns `affected_count`.

Frontend (`ui/src/app/expenses/`):

- `layout.tsx` — tab nav array. New tabs added by extending the array + creating `page.tsx` in a subdirectory.
- `components/expenses/category-edit-popover.tsx` — flat category picker, opens a dialog, search by slug/label, calls the transaction PATCH endpoint. Reusable but flat; needs upgrade to hierarchical.
- `components/expenses/transactions-table.tsx` — renders the inline `CategoryEditPopover` per row. Currently has no checkbox column.
- `lib/expenses/api.ts` — `expensesApi.patchTransactionCategory(txId, userId, slug)`. New methods will be added here.

### What's not available (and we will not add)

- A merchant aggregation endpoint. The new `GET /api/expenses/merchants` computes this with a `GROUP BY merchant_normalized` + LEFT JOIN to `merchant_category_cache` — no new tables.
- A `Merchant` model. Merchants are transaction-derived; the merchant tab is a view over aggregated transaction rows joined to cache rows.

## 4. UX outline

### 4.1 Tab nav

`ui/src/app/expenses/layout.tsx` `TABS` array becomes:

```
[Overview] [Monthly] [Transactions] [Sources] [Merchants] [Trips] [RSU]
```

Ordering rationale: Merchants sits next to Sources because both are "data administration" surfaces; the time-axis tabs (Overview, Monthly, Transactions) stay grouped at the front.

### 4.2 `/expenses/merchants` layout

**Filter bar (sticky top):**

- Search input — substring of `merchant_normalized` (case-insensitive).
- Category dropdown — hierarchical select; "Uncategorized" is a sentinel option that matches both `category_slug = 'uncategorized'` and rows with no cache mapping.
- Source filter — `All / User-confirmed / LLM-cached / Uncategorized`.
- Confidence filter — slider or `< 0.85` toggle + numeric range input.
- Preset chips — **"Needs attention"** (default; `source ≠ user AND (confidence < 0.95 OR category = uncategorized)`), **"Top spend"** (sort by total_nis desc).

**Table:**

| ☐ | Merchant | Category | Confidence | Source | # Txs | Total | Last seen | Actions |
|---|---|---|---|---|---|---|---|---|
| ☐ | שטראוס | food.groceries | 0.92 | cache (llm) | 47 | ₪3,210 | 2026-05-08 | Confirm · Edit |
| ☐ | קופיקס | Uncategorized | — | — | 12 | ₪580 | 2026-05-09 | Edit |

- Sort by any column header. Default sort: "Needs attention" descending — uncategorized first, then low-confidence non-user-confirmed, tiebreak by `tx_count` desc.
- The Category cell is a chip. Click → opens the hierarchical picker modal.
- "Confirm" action only renders when `source ≠ user`. One click → `PATCH` with `confirm: true` (locks current category as `source=user`, `confidence=1.00` without changing category).
- Row click → expands inline to show the last 5 transactions for that merchant (read-only) with a "Open all in Transactions" link.

**Bulk bar (sticky bottom, appears when ≥1 row selected):**

```
N merchants selected · 234 transactions affected   [Apply category…] [Confirm current] [Clear]
```

- **Apply category…** opens the picker, then POSTs `/merchants/bulk-category` with the selected `merchant_normalized` values + the chosen slug.
- **Confirm current** POSTs `/merchants/bulk-category` per merchant with `confirm: true` (locks all selected merchants' current categories).
- **Clear** deselects.

**Add sub-category:**

A button labeled "+ Add sub-category" in the picker modal opens a secondary dialog:

```
Parent:    [ insurance ▼ ]    (only top-level user categories listed)
Slug:      [ health     ]     → stored as `insurance.health`
Label:     [ Health Insurance ]
Label (he): [ ביטוח בריאות   ] (optional)
            [Cancel] [Add]
```

On success, the new option appears in the picker immediately (cache invalidation on `/categories`).

### 4.3 Date-range bulk-label on `/expenses/transactions`

The existing transactions page already filters by date. Two additions:

- **Selection model.** Add a checkbox column to `transactions-table.tsx`. Header checkbox = "select all rows currently rendered". A chip below the filter bar reads **"Select all matching filter (N transactions)"** — clicking it resolves the current filter (incl. `from_date` / `to_date`) into the full ID list and marks them all selected.
- **Bulk-apply bar (sticky bottom, mirrors merchant tab):**

```
N transactions selected   [Apply category…] [Add tag…] [Remove tag…] [Clear]
```

All three actions call `POST /transactions/bulk-label`. Category and tags can be combined in a single call. Range-bulk **never** writes to `merchant_category_cache` — it is per-tx by design.

### 4.4 Two shared edit modals

Two distinct modals are reused across surfaces:

**A. Hierarchical category picker** — bare picker, no tags, no fan-out checkbox. Used in the merchant tab (category chip click) and as the embedded picker inside modal B.

**B. Label editor** — picker + tag chip input + fan-out checkbox. Used in transaction inline edit and bulk-apply only.

```
A. Hierarchical picker:        B. Label editor (transactions only):
┌─ Pick category ────┐         ┌─ Set labels ──────────────────────┐
│ [ tree view ]      │         │ Category:  [ picker A embedded ]  │
│   + Add sub-cat    │         │ Tags:      [ chip input ]         │
│   [Cancel] [Save]  │         │            + trip:greece-2026 ×   │
└────────────────────┘         │ ☐ Apply to all sibling txs        │
                                │             [Cancel] [Save]      │
                                └───────────────────────────────────┘
```

Rationale: merchant-level edits don't carry per-tx tags (tags are inherently per-row), so the merchant tab uses the bare picker. Only transaction-level edits combine category + tags + fan-out choice.

The "Apply to all siblings" checkbox in the label editor:

- **Default-OFF in transactions inline edit** — small behavior change from today's implicit fan-out. Documented in §6.4.
- **Hidden in bulk-apply** — always off, per-tx by definition (bulk-apply already operates on an explicit set of transaction_ids).

### 4.5 Hierarchical picker

The picker is a modal with:

- Search input (filters by slug and `label_en` substring).
- Tree view: top-level categories expanded by default if they have children; click parent to collapse/expand.
- Both parents and children are selectable rows (parent selection is allowed — leaving the user with `housing` directly if no sub-category fits).
- Slug shown as a subtle subtitle on each row (e.g. `Food (food)`, `  Groceries (food.groceries)`).
- "+ Add sub-category" button at the bottom of the modal.

## 5. API contract

All endpoints live under `/api/expenses/` and are scoped by `user_id` (passed as a query param, matching existing conventions).

### 5.1 `GET /merchants`

**Query:** `user_id=&category=&source=&min_confidence=&max_confidence=&search=&sort=&order=&limit=`

- `category` accepts a category `slug` or the literal `uncategorized` (which matches `category_slug = 'uncategorized'` OR rows with no cache row).
- `source` ∈ `{user, llm, issuer, cache, uncategorized}`. `cache` = "in cache, source not yet user-confirmed".
- `min_confidence` / `max_confidence` ∈ `[0.00, 1.00]`.
- `search` is a case-insensitive substring of `merchant_normalized`.
- `sort` ∈ `{merchant, category, confidence, tx_count, total_nis, last_seen}`. `order` ∈ `{asc, desc}`. Default: `needs_attention desc`.
- `limit` defaults to 500, cap 1000 (no pagination in MVP; the corpus has a few hundred distinct merchants).

**Response:**

```json
{
  "merchants": [
    {
      "merchant_normalized": "שטראוס",
      "category_slug": "food.groceries",
      "category_label": "Groceries",
      "parent_slug": "food",
      "parent_label": "Food",
      "confidence": 0.92,
      "source": "llm",
      "is_cached": true,
      "tx_count": 47,
      "total_nis": 3210.50,
      "total_usd": 0.00,
      "last_seen": "2026-05-08"
    }
  ],
  "total": 142
}
```

`is_cached=false` means there is no `merchant_category_cache` row; the merchant's transactions carry per-tx categories assigned at ingest with `category_source ∈ {issuer, llm}` and `category_confidence < 0.85` (so the categorizer skipped writing a cache entry).

### 5.2 `PATCH /merchants/{merchant_normalized}`

URL path is URL-encoded UTF-8 (Hebrew merchant names round-trip via FastAPI's path parser; tested explicitly).

**Body (one of):**

```json
{ "category_slug": "food.groceries" }
```

```json
{ "confirm": true }
```

- `category_slug` — writes/updates the cache row (`source=user`, `confidence=1.00`) AND updates every `expense_transactions` row with that `merchant_normalized` for this user. Returns `affected_transactions`.
- `confirm: true` — keeps current category, just flips `source` to `user` and `confidence` to `1.00` on the cache row. If there is no cache row yet, the merchant's most-common category among its transactions is what gets cached.

Both shapes share the same response:

```json
{
  "merchant_normalized": "שטראוס",
  "category_slug": "food.groceries",
  "affected_transactions": 47,
  "cache_row_created": false
}
```

### 5.3 `POST /merchants/bulk-category`

**Body:**

```json
{
  "user_id": "ariel",
  "merchant_normalizeds": ["שטראוס", "קופיקס"],
  "category_slug": "food.groceries",
  "confirm": false
}
```

If `confirm=true`, `category_slug` is ignored and each merchant gets a confirmation pass.

**Response (per-item status; never fails the whole batch unless body is malformed):**

```json
{
  "results": [
    { "merchant_normalized": "שטראוס", "status": "ok", "affected_transactions": 47 },
    { "merchant_normalized": "קופיקס", "status": "ok", "affected_transactions": 12 }
  ],
  "ok_count": 2,
  "error_count": 0,
  "total_affected_transactions": 59
}
```

### 5.4 `POST /categories`

**Body:**

```json
{
  "user_id": "ariel",
  "parent_slug": "insurance",
  "slug": "health",
  "label_en": "Health Insurance",
  "label_he": "ביטוח בריאות"
}
```

- Slug is suffixed onto the parent: stored as `insurance.health`. The endpoint rejects bodies that already include a dot in `slug`.
- Parent must be a top-level category owned by this user (`parent_id IS NULL`, `user_id = ariel`). Rejects with 422 if parent itself has a parent (enforces one-level nesting).
- Duplicate full slug returns 409.
- `label_he` is optional; if missing, falls back to `label_en` for display.
- `is_excluded_from_spend` and `is_inflow` are inherited from the parent.

**Response:** the created `CategoryOut` row.

### 5.5 `PATCH /transactions/{transaction_id}` — behavior change

Body gains an optional `apply_to_siblings: bool` field. Default behavior:

- **From the inline-edit UI on `/expenses/transactions`:** explicit `apply_to_siblings: false` is sent. Only this transaction is updated; the cache row is NOT touched.
- **From the merchant tab (via the shared service):** `apply_to_siblings: true`. Fans out as today.
- **Omitting the field entirely:** preserves the current behavior (`apply_to_siblings: true`) so any external caller / test that doesn't send it sees no regression.

The endpoint docstring documents this contract. A regression test asserts both shapes.

### 5.6 `POST /transactions/bulk-label`

**Body:**

```json
{
  "user_id": "ariel",
  "transaction_ids": [1234, 1235, 1236],
  "category_slug": "insurance.travel",
  "add_tags": ["trip:greece-2026-aug"],
  "remove_tags": []
}
```

- `category_slug`, `add_tags`, `remove_tags` are all optional but at least one must be present (422 if all missing).
- `category_slug`, if present, writes `category_id` on each named row. Does NOT touch `merchant_category_cache`.
- `add_tags` appends to each row's `tags` JSON array (dedup'd); `remove_tags` strips them.
- Returns `{affected: N, skipped: [{tx_id, reason}, ...]}`. Unknown tx_ids land in `skipped` rather than aborting.

## 6. Data model

No schema changes. The model below names the columns this wave reads or writes; nothing new is added.

### 6.1 Existing columns this wave depends on

- `merchant_category_cache(user_id, merchant_pattern, is_regex, category_id, source, confidence, hit_count, last_hit_at)`.
- `expense_categories(user_id, slug, label_en, label_he, parent_id, is_excluded_from_spend, is_inflow, display_order)`.
- `expense_transactions(merchant_normalized, category_id, category_source, category_confidence, tags, occurred_on, amount_nis, amount_orig_currency)`.

### 6.2 Aggregation query for `GET /merchants`

Pseudo-SQL:

```sql
SELECT
  t.merchant_normalized,
  COALESCE(c.slug, 'uncategorized') AS category_slug,
  COALESCE(c.label_en, 'Uncategorized') AS category_label,
  COALESCE(parent.slug, NULL) AS parent_slug,
  COALESCE(parent.label_en, NULL) AS parent_label,
  COALESCE(m.confidence, AVG(t.category_confidence)) AS confidence,
  COALESCE(m.source, 'uncached') AS source,
  m.id IS NOT NULL AS is_cached,
  COUNT(*) AS tx_count,
  SUM(CASE WHEN t.currency = 'ILS' THEN t.amount_nis ELSE 0 END) AS total_nis,
  SUM(CASE WHEN t.currency = 'USD' THEN t.amount_orig_currency ELSE 0 END) AS total_usd,
  MAX(t.occurred_on) AS last_seen
FROM expense_transactions t
LEFT JOIN merchant_category_cache m
  ON m.user_id = t.user_id AND m.merchant_pattern = t.merchant_normalized AND m.is_regex = FALSE
LEFT JOIN expense_categories c
  ON c.id = COALESCE(m.category_id, t.category_id)
LEFT JOIN expense_categories parent
  ON parent.id = c.parent_id
WHERE t.user_id = :user_id
  AND (...filters...)
GROUP BY t.merchant_normalized, c.slug, c.label_en, parent.slug, parent.label_en, m.confidence, m.source, m.id
ORDER BY ... LIMIT 500;
```

`is_cached` and `source='uncached'` together flag "merchants the categorizer has seen but never cached a rule for" — these are typical "Needs attention" rows.

### 6.3 Service extraction: `apply_merchant_category`

```python
def apply_merchant_category(
    session: Session,
    *,
    user_id: str,
    merchant_normalized: str,
    category_slug: str | None,         # None when confirm=True
    confirm: bool = False,
    source: str = "user",
    confidence: Decimal = Decimal("1.00"),
) -> ApplyResult:
    """
    Single source of truth for "user has decided what category this merchant is".
    Writes/updates merchant_category_cache (source, confidence, category_id).
    Updates every expense_transactions row for this user with this merchant_normalized.
    Returns (cache_row_created, affected_transactions, resolved_category_slug).
    """
```

Existing `PATCH /transactions/{id}` (in fan-out mode) and the new `PATCH /merchants/{merchant_normalized}` both go through this function. Tests target the function directly; the endpoints become thin wrappers.

### 6.4 Behavior change on `PATCH /transactions/{id}`

`apply_to_siblings` is a new optional body field with default `True` server-side (back-compat). The inline transactions UI sends `false` explicitly going forward; the merchant tab and bulk-category endpoint use the service directly without going through the transaction endpoint. Tests:

- `test_patch_transaction_default_fans_out` — body without `apply_to_siblings` still hits all siblings.
- `test_patch_transaction_no_fanout` — `apply_to_siblings: false` updates only the named row, does not write to cache.

This is the only "soft" behavior change in this wave and is documented in the endpoint docstring and the EX8 entry of the SDD handover.

## 7. Edge cases & error handling

- **Hebrew merchant names in URL paths.** `PATCH /merchants/{merchant_normalized}` accepts UTF-8 URL-encoded values. Test fixture: `שטראוס`. FastAPI handles encoding/decoding transparently.
- **Cross-currency totals.** A merchant with mixed NIS + USD activity returns both `total_nis` and `total_usd`; the UI renders both lines in the Total cell when non-zero. No FX conversion (per Ariel: out of scope).
- **Refund-only merchants.** Refunds are excluded from categorization but are aggregated into the merchant row. Picking a category applies to the refund rows too via the existing service path.
- **`merchant_normalized` collisions across users.** Every query scopes by `user_id`. No cross-tenant leakage.
- **Picker cache after sub-category add.** Frontend invalidates the `/categories` query on `POST /categories` success so the new option appears immediately.
- **Bulk endpoint partial failure.** Per-item status; `200 OK` for the whole call. UI shows a toast: "Applied to X merchants. Y failed — show details."
- **Duplicate sub-category slug.** `409 Conflict` with a body explaining the existing slug. Picker shows the conflict inline.
- **Adding a sub-category under a category with existing transactions.** Fine — creates an empty child. Existing transactions stay where they are; user re-categorizes individually if desired.
- **Parent not top-level.** `422 Unprocessable Entity`. MVP enforces one nesting level.
- **`tags` array max size.** No explicit cap, but `bulk-label` dedups within each row; pathological tag spam would only inflate the JSON column.
- **Unknown `transaction_ids` in `bulk-label`.** Listed in `skipped` rather than aborting the batch.
- **`PATCH /merchants/{merchant}` with no transactions for that merchant.** The merchant doesn't exist for this user → `404`.
- **`confirm: true` with no existing cache row AND no transactions.** Edge case (no data to lock in) → `404`.
- **`confirm: true` with no cache row but with multiple transactions assigned to different categories.** The most-common category among them is what gets cached; ties broken by most-recent `occurred_on`.

## 8. Performance

- `GET /merchants` runs one GROUP BY query plus joins. With ~2,200 transactions and a few hundred distinct merchants, this is sub-100ms. Defensive `LIMIT 1000`.
- `POST /merchants/bulk-category` iterates the service per merchant; each call is one cache UPSERT + one bulk transaction UPDATE keyed on `merchant_normalized`. ~10ms per merchant; a 50-merchant bulk is well under a second.
- `POST /transactions/bulk-label` runs a single `UPDATE … WHERE id IN (:ids)` plus a JSON-modify pass for tags (handled in Python where SQLite JSON1 is awkward).
- Categories taxonomy is preloaded on tab mount and cached in the SWR/react-query store. Adding a sub-category invalidates one key.

## 9. Testing

### Backend (pytest, `not llm_eval` lane)

- `test_apply_merchant_category_service.py` — unit tests on the extracted service: new merchant + new cache row; existing merchant + cache row update; idempotent re-apply; refund rows updated; cross-tenant scoping.
- `test_merchants_endpoint.py` — aggregation correctness; filters (category, source, min/max confidence, search); sort variants; "uncategorized" matches both empty cache + literal slug.
- `test_merchant_patch.py` — `category_slug` path; `confirm: true` path; 404 on unknown merchant; Hebrew merchant name fixture.
- `test_bulk_category.py` — happy-path multi-merchant; partial failure surfaces in `results[]`; affected counts.
- `test_categories_post.py` — happy path; duplicate slug 409; parent-must-be-top-level 422; slug containing a dot 422; per-user scoping (user A's add doesn't appear for user B).
- `test_transactions_bulk_label.py` — category-only; tags-only; combined; rejects empty body; unknown tx_id surfaces in `skipped`; does NOT write to `merchant_category_cache`.
- `test_patch_transaction_fanout_flag.py` — regression: default still fans out; explicit `false` does not.

### Frontend

- TypeScript build + ESLint must stay clean.
- No new unit tests (per standing preference; manual smokes skipped).
- The dev server pages must load without console errors (verified by Ariel on review).

### Audit-corpus CLI (existing tooling)

- Re-run `argosy expenses audit-corpus --user-id ariel --dir "D:/Google Drive/Family/Finances/Portfolio/Resources"` after merging. No counts should change (this wave is read-mostly + targeted writes; no parser changes).

## 10. Open items / assumptions

These are assumptions I'm making absent contrary input from Ariel. Override during plan review if any are wrong.

1. **One-level nesting only in MVP.** `insurance.health` allowed, `insurance.health.dental` rejected.
2. **Picker shows per-user categories only.** System defaults (`user_id IS NULL`) are already seeded per-user; the picker queries the user-owned rows.
3. **Default sort is "Needs attention" desc.** Uncategorized first, then low-confidence non-user-confirmed, tiebreak by tx_count desc.
4. **Merchant table groups by `merchant_normalized` only.** No fuzzier clustering. Variants that normalize differently appear as separate rows.
5. **"Confirm current" with no cache row falls back to the most-common category among the merchant's transactions** (tie-broken by most-recent `occurred_on`).
6. **Range-bulk on transactions resolves "select all matching filter" client-side** by paginating `GET /transactions` until exhausted, then POSTs the ID list. No new filter-based bulk endpoint.

## 11. Future work (explicitly deferred)

- Top-level category creation.
- Multi-level category nesting.
- Category rename, merge, delete.
- Merchant rename / merging variants under one canonical name.
- Bulk-by-filter endpoint (`POST /transactions/bulk-label` accepts a filter spec instead of an ID list) — UI cost only matters above ~10k transactions per range.
- Regex pattern editor for `merchant_category_cache`.
- Auditing: a separate `/expenses/changelog` of who-changed-what-when.
- LLM-assisted "suggest a category" inline on uncategorized rows (existing categorizer agent could be reused).
- "Split merchant" mechanic — would only become useful if the BIT→cash collapse turns out to materially distort totals (Ariel's stated read: it won't).

## 12. Files touched (rough preview, for plan-writing)

Backend:
- `argosy/services/expense_categorize/category_resolver.py` (or a new `merchant_service.py`) — extract `apply_merchant_category`.
- `argosy/api/routers/expenses.py` — add `GET /merchants`, `PATCH /merchants/{merchant_normalized}`, `POST /merchants/bulk-category`, `POST /categories`, `POST /transactions/bulk-label`; modify `PATCH /transactions/{id}` to honor `apply_to_siblings`.
- `argosy/api/schemas/expenses.py` (or wherever Pydantic models live) — `MerchantOut`, `MerchantListOut`, `MerchantPatchIn`, `BulkCategoryIn`, `BulkCategoryOut`, `CategoryCreateIn`, `BulkLabelIn`, `BulkLabelOut`.
- `tests/test_apply_merchant_category_service.py`, `tests/test_merchants_endpoint.py`, `tests/test_merchant_patch.py`, `tests/test_bulk_category.py`, `tests/test_categories_post.py`, `tests/test_transactions_bulk_label.py`, `tests/test_patch_transaction_fanout_flag.py`.

Frontend:
- `ui/src/app/expenses/layout.tsx` — add `Merchants` tab.
- `ui/src/app/expenses/merchants/page.tsx` (new) — server-rendered shell.
- `ui/src/components/expenses/merchants-table.tsx` (new) — the table + filter bar + bulk bar.
- `ui/src/components/expenses/category-picker-hierarchical.tsx` (new) — replaces `category-edit-popover.tsx` flat dialog. Old file is deleted; the new picker is used directly by the merchant tab and embedded inside the label editor (B).
- `ui/src/components/expenses/label-editor.tsx` (new) — the shared "set labels" modal for transaction edits (inline + bulk).
- `ui/src/components/expenses/add-subcategory-dialog.tsx` (new) — modal for sub-cat creation.
- `ui/src/components/expenses/transactions-table.tsx` — add checkbox column; wire bulk bar; switch inline edit to default-no-fanout via `LabelEditor`.
- `ui/src/lib/expenses/api.ts` — `listMerchants`, `patchMerchant`, `bulkApplyCategory`, `createCategory`, `bulkLabelTransactions`, `fetchTransactionIdsMatchingFilter`.

SDD:
- `docs/design/SDD.md` — append EX8 entry under §13 phasing, update the handover note when this wave lands.

## 13. Definition of done

- All seven backend test files exist and pass under `pytest -m "not llm_eval"`.
- Total test count is at least 1,035 + the new tests (no regressions).
- `tsc` and `eslint` pass on the `ui/` workspace.
- The Merchant tab renders without console errors and surfaces every merchant in the corpus, sorted by "Needs attention" by default.
- Adding a sub-category, confirming a merchant, bulk-applying a category, and range-bulk-adding a trip tag are all exercised by Ariel manually before merge.
- The SDD handover note in `docs/design/SDD.md` is updated to reflect EX8 landing.
