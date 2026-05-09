# Wave EX4 — Expenses Dashboard

| Field | Value |
|---|---|
| **Wave** | EX4 |
| **Date** | 2026-05-09 |
| **Status** | Spec — autonomous draft (user authorized "do everything, I'll review tomorrow") |
| **Authors** | Claude (autonomous) |
| **Predecessor waves** | EX1 (ingest core, landed), EX1.1 (stabilization, in flight) |
| **Successor waves** | EX2 (anomaly detection — backend), wave-2 data verification (deferred) |

---

## 1. Purpose

The user has accumulated 700+ household-expense transactions across 5 sources (Leumi bank, Isracard 1266 + 0235, Max 6225, Discount 2923) plus FX support, foreign-currency rows, refund matching, bank↔card correlation. This wave builds a **substantive Next.js dashboard** at `/expenses` that surfaces the data with charts, drilldowns, and per-currency breakdowns. The user's stated goal: "lots of visualizations, plots, stats… make it look impressive and insightful — we have a lot of data — let's show it."

End-state: opening `/expenses` reveals at-a-glance health of household finances; one click drills into any category, source, or month.

## 2. Scope

### In scope

- New top-level `/expenses` route with three sub-pages (overview, transactions, sources).
- "Expenses" entry added to the global NavBar.
- 8+ visual components built on Recharts 3.8 (already installed): monthly-spend chart, category donut, category bar-chart, source breakdown, monthly comparison, top-merchants, refund timeline, anomaly highlights.
- Searchable + filterable transactions table with inline category re-assign (PATCH endpoint already exists).
- FX toggle: per-currency breakdown vs. NIS-converted (using BoI cache via `argosy.services.fx`).
- API additions where strictly necessary to power the dashboard (kept minimal).
- A small backend hotfix: `TransactionOut.amount_nis` is currently non-optional but post-EX1.1 foreign rows are `None` — fix to `float | None` before the dashboard hits the endpoint.
- Smoke + visual sanity tests (Playwright not in scope; rely on manual smoke + unit tests for hooks/utils).

### Out of scope (explicitly)

- **Wave 2 data verification with the user** — that's a separate flow with manual review.
- **EX2 anomaly detection backend** — recurring-missed flag, novel-merchant alerts. Wave EX4 surfaces what's already in the schema; EX2 adds new agents.
- **Mobile responsiveness audit beyond Tailwind defaults** — not first-class but won't actively break.
- **Internationalization (Hebrew RTL)** — categories already have `label_he` field but UI ships English-first.
- **Drag-to-categorize, bulk operations, CSV export** — out for v1.
- **Authentication beyond existing NextAuth** — single-user Argosy.

## 3. Background

### What's available right now (post-EX1.1 backend)

REST endpoints under `/api/expenses/*`:
- `GET /sources?user_id=…` → list of `{id, kind, issuer, external_id, display_name, cardholder_name, active}`
- `GET /transactions?user_id=…&from_date=…&to_date=…&category=…&source_id=…&direction=…&include_card_payments=…&search=…&limit=…&offset=…` → `{transactions: [...], total: N}`
- `PATCH /transactions/{id}` `{user_id, category_slug}` → `{transaction_id, category_slug, category_source, affected_count}`
- `GET /categories?user_id=…` → list of category metadata (`label_en/he`, `parent_slug`, `is_excluded_from_spend`, `is_inflow`)
- `GET /monthly-summary?user_id=…&months=N` → list of `{month, totals_by_currency: {NIS: …, USD: …}, transaction_count}`
- `POST /upload` `(files, user_id, card_last4?)` → multi-file ingestion

WebSocket events: `expense.statement.parsed`, `expense.statement.failed`. Optional to subscribe in v1.

### What's NOT available (and we will not add unless trivial)

- Anomaly detection (uncategorized, novel-merchant, large outlier, fee-waiver-missed). Some of these we can compute client-side from the transactions list.
- Recent-refunds endpoint. Computable from `direction=credit`.
- Top-merchants endpoint. Computable client-side from a wide transactions fetch.
- Reconciliation status per source. Computable from latest statement's conservation gap (we'll add the bare-minimum endpoint if needed).

## 4. UX outline

### 4.1 Navigation

NavBar gains an "Expenses" entry between "Portfolio" and "Plan":

```
Home | Advisor | Portfolio | Expenses | Plan | Proposals | Argonaut | Agents | Files | Audit | Domain KB | Settings
```

### 4.2 Three sub-pages

#### `/expenses` — Overview

Hero card row (4 small cards):
- **This month** — total NIS spend (NIS-converted if FX toggle ON; sum-of-NIS-only otherwise) with sparkline of last 6 months. Up-arrow / down-arrow vs. last month.
- **Top category** — name + NIS + percent of monthly spend.
- **Sources** — count of active + cards linked. "5 sources, 4 cards reconciled."
- **Recent refunds** — count + total NIS this month.

Charts row (2 large cards):
- **Monthly spend (12 months)** — Recharts BarChart, stacked by category (top 5 + "other"). Hover → tooltip with category breakdown. Toggle: per-currency lines vs NIS-converted bars.
- **Category donut** — Recharts PieChart of current month's spend by category. Click slice → drill into `/expenses/transactions?category=<slug>&from_date=<month-start>`.

Insights row (2 cards):
- **Top merchants this month** — Top 10 by spend, with category badge. Click row → filter transactions.
- **Anomaly highlights** — generated client-side from data:
  - Uncategorized count → click to triage.
  - Novel merchants this month (merchant unseen in prior 6 months).
  - Large transactions (≥ ₪500 single, in non-utility categories).
  - Card 2923 fee-waiver: if Discount card-fee row in the latest statement isn't matched by a discount-rebate row → flag (per `project_card_2923_fee_waiver.md`).

Footer card:
- **Sources & reconciliation** — table of 5 sources with last statement period, parsed_total, declared_total, gap (₪), correlated card-payment count. Status pill: green ✓ if gap < ₪0.50, yellow if 0.50 ≤ gap < 5, red otherwise.

#### `/expenses/transactions` — Browse

Top of page: filter bar with search + period range + category multi-select + source multi-select + direction toggle (debit / credit / all) + "show card-payments" toggle.

Table:
- Columns: Date | Merchant | Category (with confidence badge) | Source | Amount (NIS or original currency) | Refund-of (linked tx if applicable).
- Inline category edit: click category badge → popover with all categories; selecting one calls `PATCH /transactions/{id}` and updates row + siblings.
- Pagination (offset/limit).
- "Export CSV" button — out of scope for v1; placeholder hidden.

URL state mirrors filters (`?category=…&from_date=…`) so charts can deep-link.

#### `/expenses/sources` — Per-source detail

For each source:
- Card with statement timeline (12 months, height proportional to debit-total).
- Per-statement table: period, parsed_total, declared_total, gap, transaction count, correlation count (for cards).
- Show parser version (`parser_name 0.1.0`).
- "Re-ingest this source" button — not implemented v1; placeholder.

### 4.3 FX toggle behavior

Global state stored in `localStorage` (`argosy.expenses.fxMode`): `"per_currency"` (default) or `"nis_converted"`.

- `per_currency` mode: foreign rows render with `amount_orig + currency_orig` ("$25.00 USD"). Charts show per-currency lines stacked.
- `nis_converted` mode: foreign rows render `~₪92` (with hover tooltip showing original "$25 @ 3.68"). Charts show single NIS-converted total. **Conversion happens server-side** through a new endpoint to keep the client simple — see §5.

## 5. New backend endpoints

Add only what we strictly need to make the dashboard work without 5+ round-trips per page.

### 5.1 `GET /api/expenses/dashboard-overview?user_id=…&months=12&fx=per_currency|nis`

Single endpoint that pre-aggregates everything the overview page needs:

```python
class DashboardOverview(BaseModel):
    months: list[MonthlyTotalEntry]                  # already exists shape
    current_month_top_categories: list[CategorySpend]
    top_merchants_current_month: list[MerchantSpend]
    anomalies: list[AnomalyCard]
    sources_health: list[SourceHealthEntry]
    fx_mode: str                                      # echoed back
```

`AnomalyCard` is a small DTO: `{kind: str, severity: 'red'|'yellow'|'info', message: str, link: str | None}` where `kind` is one of `uncategorized | novel_merchant | large_outlier | fee_waiver_missed | conservation_gap`.

`SourceHealthEntry`: `{source_id, display_name, last_period, parsed_total, declared_total, gap, status, correlated_card_payments}`.

Why one endpoint vs. four: (1) avoids waterfall on first paint, (2) gives the backend the option to compute FX conversions server-side instead of bouncing through the wire twice. Backwards-compat: existing `/sources`, `/transactions`, `/categories`, `/monthly-summary` stay unchanged.

### 5.2 `GET /api/expenses/dashboard-overview` server-side FX

When `fx=nis` and the corpus has foreign rows, the endpoint calls `argosy.services.fx.convert(session, amount_orig, currency_orig, "ILS", occurred_on)` per row and presents NIS-only sums. Cache misses fall back to "no rate available; row excluded with a warning in the response payload".

### 5.3 `GET /api/expenses/source-detail/{source_id}?user_id=…`

For `/expenses/sources`:

```python
class SourceDetail(BaseModel):
    source: SourceOut
    statements: list[StatementSummary]   # period_start, period_end, parsed_total, declared_total, status, parser_name, transaction_count, correlated_count
```

### 5.4 Hotfix — `TransactionOut.amount_nis` becomes `float | None`

Today (post-EX1.1) the parser stores NULL for foreign rows but `TransactionOut` is `amount_nis: float`. `float(r.amount_nis)` will TypeError. Fix to `float | None` and convert with `... if r.amount_nis is not None else None`. Also add `amount_orig: float | None` and `currency_orig: str | None` to the DTO so the UI can render either.

## 6. Frontend architecture

### 6.1 File structure

```
ui/src/app/expenses/
  layout.tsx                    # Tabs: Overview | Transactions | Sources + FX toggle
  page.tsx                      # Overview
  transactions/
    page.tsx                    # Browse + filter
  sources/
    page.tsx                    # Per-source detail
ui/src/components/expenses/
  monthly-spend-chart.tsx
  category-donut.tsx
  top-merchants-card.tsx
  anomaly-highlights.tsx
  sources-health-table.tsx
  source-statement-timeline.tsx
  transactions-table.tsx
  category-edit-popover.tsx
  fx-toggle.tsx
  hero-stats.tsx
ui/src/lib/expenses/
  api.ts                        # Typed fetch helpers (extends @/lib/api)
  format.ts                     # NIS/USD formatters, percent, relative-month
  fx-mode.ts                    # localStorage hook for fx mode
```

### 6.2 Component contracts

Each chart component takes typed `data` props and a `loading` flag — no internal fetching. Page components handle fetch + hand off data. This keeps each component independently testable and previewable.

Example signature:

```ts
interface MonthlySpendChartProps {
  data: MonthlyTotalEntry[];           // from /monthly-summary
  fxMode: "per_currency" | "nis";
  height?: number;
}
```

### 6.3 Recharts conventions

- Use `ResponsiveContainer` always.
- Color palette: HSL-based, derived from category slug hash so colors are stable across page loads.
- Tooltips: custom `<ChartTooltip>` component matching the existing card aesthetic.
- Animations off (Recharts default 1500ms is distracting).

### 6.4 State management

- React state per page; no global store.
- localStorage for FX mode + last-used filter set.
- URL query params mirror filters so deep links + back-button work.

## 7. Implementation strategy

### 7.1 Build order

1. Backend hotfix (`amount_nis: float | None`) + new `/dashboard-overview` + `/source-detail` endpoints + their tests.
2. Frontend scaffolding: routes, layout, NavBar entry, FX toggle component, API helpers.
3. Components: hero stats → monthly chart → category donut → top merchants → anomaly highlights → sources health table.
4. `/transactions` page: table + filters + inline category edit.
5. `/sources` page: source-detail view.
6. Manual smoke + screenshot pass.

### 7.2 Risk controls

- **Backend hotfix is mandatory before any UI work** — current main has a TypeError waiting to fire if we hit the transactions endpoint with foreign rows in the DB.
- **The dashboard-overview endpoint must handle empty / partial data gracefully** — corpus may still be ingesting when user opens the page.
- **FX-conversion should never block a chart render** — if BoI cache misses for a date, return the raw row with `nis_unavailable=true` and let the UI degrade gracefully.

## 8. Tests

### 8.1 Backend

- Unit: `test_dashboard_overview_endpoint.py` — empty corpus, single-month, multi-month, mixed-currency.
- Unit: `test_source_detail_endpoint.py` — non-existent source 404, valid source returns full timeline.
- Hotfix: extend `test_expense_routes.py::test_list_transactions_includes_foreign_rows` — assert response shape allows `amount_nis: null`.
- FX integration: stub `fx.convert` to return deterministic rates; assert dashboard-overview returns NIS sums correctly.

### 8.2 Frontend

- Vitest already-in-tree? Check during impl.
- Component tests for the format / fx-mode hooks.
- Manual smoke: open each route in dev browser, no console errors, charts render, FX toggle changes display.

## 9. Acceptance criteria

- All 4 backend endpoints exist + return shape matches the spec.
- `GET /api/expenses/transactions` no longer TypeErrors on foreign rows.
- NavBar has "Expenses" tab between Portfolio and Plan.
- `/expenses` overview page renders without errors against the post-T16 corpus.
- Charts: monthly spend, category donut, top-merchants, anomaly highlights, sources-health table — all populated.
- Transactions page: table renders 200+ rows; filter by category works; inline category edit persists.
- Sources page: per-source statement timeline + reconciliation table.
- FX toggle changes display between per-currency and NIS-converted modes.
- All new tests pass; existing 940+ test suite unaffected.

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Backfill not complete when UI dev starts | UI uses real endpoints; partial data shows partial charts. No special handling needed. |
| Recharts API differs from training data | Build small spike component first, verify in browser. |
| Next.js 16 has breaking changes from Next 14 (training-era) | UI's CLAUDE.md says read `node_modules/next/dist/docs/` first. Adopt observed conventions from `app/portfolio/page.tsx`. |
| NIS-conversion blows up because BoI cache is empty | Endpoint warm-caches a year of rates on first call; graceful fallback to raw values otherwise. |
| Inline category edit triggers cascading siblings updates that aren't reflected in UI | After PATCH, refetch the visible page (cheap; ≤200 rows). |
| Per-currency vs NIS-converted causes visual jank between modes | Animations off; values change in place. |

## 11. Plan handoff

Plan to be written by `superpowers:writing-plans` skill. Inputs:
- This spec.
- The EX1.1 spec for context.
- The current `argosy/api/routes/expenses.py` for endpoint patterns.
- `ui/src/app/portfolio/page.tsx` for component-style baseline.

Plan should produce ordered tasks, each with explicit per-task acceptance + rollback. Plan execution will use the same subagent pattern as EX1.1.

## 12. Decisions log (autonomous, since user authorized "no questions")

| # | Decision | Choice | Rationale |
|---|---|---|---|
| A1 | API additions | Add `/dashboard-overview` + `/source-detail` | Avoids 5-round-trip waterfall on first paint; one trip suffices for overview page. |
| A2 | FX conversion location | Server-side when `fx=nis` requested | Keeps client simple; lets backend cache results per-request. |
| A3 | Anomaly detection in v1 | Compute client-side from already-fetched data | Defers EX2 until anomaly definitions stabilize. |
| A4 | Hebrew RTL support | Defer | Existing UI is English-first; user is bilingual but uses English UI. |
| A5 | Chart library | Recharts 3.8 (already installed) | Avoids new dep; it's the project's default. |
| A6 | Inline category edit | Yes, popover with all categories | Critical UX; the cascading-siblings PATCH already exists. |
| A7 | URL state mirroring | Yes | Deep links + back-button + chart drill-through depend on it. |
| A8 | Card 2923 fee-waiver flag | Surfaced as anomaly card on overview | Honors `project_card_2923_fee_waiver.md` memory. |
| A9 | Mobile / responsive | Tailwind defaults, no extra layout work | Single-user, used on desktop. |
| A10 | Backend hotfix | Land FIRST before any UI work | Prevents TypeError on foreign-row queries. |
