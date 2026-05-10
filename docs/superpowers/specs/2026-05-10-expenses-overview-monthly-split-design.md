# Expenses Overview / Monthly Split

| Field | Value |
|---|---|
| **Wave** | EX6 (UI rework — first of two parallel specs; the other is EX7 categorization correctness) |
| **Date** | 2026-05-10 |
| **Status** | Spec — drafted from brainstorm dialog with Ariel; awaiting user review |
| **Authors** | Claude + Ariel |
| **Predecessor waves** | EX4 (dashboard v1, landed), EX4.x (UX iteration, landed), EX5 (income split, trips, dividends/taxes, anomaly oddities — landed) |
| **Sibling spec** | EX7 categorization correctness (separate spec; runs in parallel) |

---

## 1. Purpose

The current `/expenses` Overview page packs nine widgets on a single screen and overloads the `?month=YYYY-MM` URL parameter to do double duty: select a month for per-month widgets AND drive the focal monthly chart. This works, but two distinct user intents are crammed together — "how is the year going?" and "what happened in <specific month>?" — so neither tab is as insight-dense as it could be.

This wave splits Overview into two tabs:

- **Overview tab** (`/expenses`) — answers *"how is the year going?"* with yearly aggregates, trailing-12 trends, and source health.
- **Monthly tab** (`/expenses/monthly`) — answers *"what happened in this month?"* with hero stats + MoM deltas, top categories/merchants, anomalies, and a 12-month chart centered on the selected month.

Each tab gains insight-level widgets that don't exist today (savings-rate trend, top movers, currency mix, MoM deltas, categories-vs-typical, largest transactions).

## 2. Scope

### In scope

- New `/expenses/monthly` route + new tab in the Expenses layout nav.
- Reshape `GET /api/expenses/dashboard-overview` to return Overview-only data.
- New `GET /api/expenses/dashboard-monthly?month=` endpoint for the Monthly tab.
- Five new widget components: `SavingsRateTrend`, `TopMoversCard`, `CurrencyMixCard`, `CategoriesVsTypicalCard`, `LargestTransactionsCard`.
- Augment `DividendsCard` and `TaxesCard` with inline 12-bar mini-charts.
- Add `mode` props to `HeroStats` (yearly trailing-12 vs monthly+MoM) and `MonthlySpendChart` (small read-only on Overview vs focal sliding-window on Monthly).
- ±6-month sliding-window chart logic, computed server-side.
- Cross-tab navigation: Overview chart bar click → `/expenses/monthly?month=YYYY-MM`; Monthly chart bar click → re-scope Monthly in place (existing behavior).
- Backend tests for every new aggregation. API tests for both endpoint shapes. One e2e test for the cross-tab click navigation.

### Out of scope

- **No schema changes / no migrations.** All data already exists in `expense_transactions` + the existing endpoints' source queries.
- **No server-side FX conversion** — the existing toggle still returns per-currency totals (open item §16-5 in SDD; not blocking this wave).
- **No mobile-responsive audit** beyond Tailwind defaults.
- **No new categorization logic** — that lives in the EX7 sibling spec.
- **No persisted user preferences** for tab/window choice — URL params remain the source of truth.
- **First-time-merchants and per-source-split widgets** — discussed and rejected by Ariel as overlapping/non-actionable.

## 3. Background

### What's available right now

REST endpoints relevant to this wave (under `/api/expenses/*`):
- `GET /dashboard-overview?user_id=&months=N&fx=&month=&window=` — currently returns `months[]`, `yearly_summary`, `current_month`, `current_month_top_categories`, `top_merchants_current_month`, `anomalies`, `dividends`, `taxes`, `sources_health`, plus various `current_month_*` hero fields.
- `GET /transactions?user_id=&from_date=&to_date=&category=&direction=…` — used by the Transactions tab; we'll reuse for `largest_transactions` server-side via direct query.
- `GET /income-breakdown?user_id=&month=` — used by the income drilldown page.

Frontend layout: `ui/src/app/expenses/layout.tsx` defines tab nav: `Overview | Transactions | Sources | Trips | RSU`. Overview lives at `ui/src/app/expenses/page.tsx`.

Confidence-bearing components (these are already mode-friendly or trivially refactorable):
- `HeroStats` — currently takes `overview: DashboardOverview` and renders monthly hero. Will be split into a `mode` prop.
- `MonthlySpendChart` — currently takes `data, fxMode, selectedMonth, onMonthSelected`. Adding a `mode: 'small' | 'focal'` prop (or splitting into two thin wrappers).
- `DividendsCard` / `TaxesCard` — currently render single-number summaries. Will accept an optional `trend12mo: list[MonthValue]` prop.

### What's not available (and we will not add)

- A categories aggregation by month-and-category for the entire trailing 12+ months. The new endpoints will compute this in the same query that already runs for `current_month_top_categories` — just expanded to the full window.
- Per-category rolling stats. We'll compute z-scores in Python on the API side; no new tables.

## 4. UX outline

### 4.1 Tab nav

`ui/src/app/expenses/layout.tsx` `TABS` array becomes:

```
[Overview] [Monthly] [Transactions] [Sources] [Trips] [RSU]
```

Ordering rationale: the two main lenses on the data come first; drill-downs follow.

### 4.2 Overview tab (`/expenses`) — "how is the year going?"

Single window-toggle (`trailing_12 | calendar_year`) at the top right governs **all** widgets on this tab (today it only governs `YearlySummaryCard`).

Widget order, top to bottom:

1. **`HeroStats` (yearly mode)** — trailing-12 totals: Spent / Income / Refunds / Statements reconciled / Anomalies. No MoM delta annotations.
2. **`YearlySummaryCard`** — unchanged; shows all categories sorted desc.
3. **`SavingsRateTrend`** — new; tiny line/area chart of `(income − spending) / income` per month for the trailing-12 window. Shows the single most important household-finance number trending up or down.
4. **`TopMoversCard`** — new; paired horizontal bars: top 5 categories that grew most + top 5 that shrank most when comparing trailing-6 vs prior-6 months (or trailing-12 vs prior-12 if `window=calendar_year` is selected and there's data for both halves).
5. **`CurrencyMixCard`** — new; stacked bar trailing-12, NIS vs USD spending. Each month is a stack.
6. **`DividendsCard` (augmented)** + **`TaxesCard` (augmented)** — side-by-side, each with an inline 12-bar mini-chart showing how the metric moved over the trailing 12 months.
7. **`MonthlySpendChart` (small mode)** — read-only height (e.g. 120px); clicking a bar navigates to `/expenses/monthly?month=YYYY-MM`. No re-scope-in-place.
8. **`SourcesHealthTable`** — unchanged.

### 4.3 Monthly tab (`/expenses/monthly`) — "what happened in this month?"

URL: `/expenses/monthly?month=YYYY-MM` (default = latest month with data).

Widget order, top to bottom:

1. **`MonthPicker`** — top right; drives the page.
2. **`HeroStats` (monthly+MoM mode)** — selected-month totals with delta annotations: each metric shows `₪X — +12% vs prior month, +5% vs trailing-12 avg`. Up/down arrow + green/red color.
3. **`MonthlySpendChart` (focal mode)** — full height (e.g. 280px); 12 bars centered on selected month per the **A-rule** (see §5.2). Clicking a bar re-scopes the page in place via `?month=`.
4. **`CategoryDonut`** + **`CategoriesVsTypicalCard`** — side-by-side. Donut shows top categories for selected month (existing). New `CategoriesVsTypicalCard` shows the 2–3 categories most divergent from the trailing-12 norm: `Groceries: ₪3,800 — 35% over your usual ₪2,800 (z = +1.8)`.
5. **`TopMerchantsCard`** + **`LargestTransactionsCard`** — side-by-side. Top merchants existing; new `LargestTransactionsCard` shows top 5 transactions by `|amount_nis|` for the selected month, with merchant + date + amount + click-through to `/expenses/transactions/<id>` (or `?search=`).
6. **`AnomalyHighlights`** — scoped to selected month, unchanged.

### 4.4 Cross-tab navigation rules

| User action | Result |
|---|---|
| On Overview, click a bar in `MonthlySpendChart (small)` | Navigate to `/expenses/monthly?month=YYYY-MM` |
| On Monthly, click a bar in `MonthlySpendChart (focal)` | Re-scope Monthly in place via `?month=` (no navigation) |
| On Monthly, click "Open transactions" link in chart header | Navigate to `/expenses/transactions?month=YYYY-MM` (existing behavior) |
| On either tab, click a category in donut/movers/categories-vs-typical | Navigate to `/expenses/transactions?category=<slug>&month=…` (existing pattern) |

## 5. API design

### 5.1 `GET /api/expenses/dashboard-overview` (reshaped)

**Query params:**
- `user_id` (required)
- `months` (default 12) — trailing-window size for `months[]`, `currency_mix`, etc.
- `fx` (`per_currency` | `nis`) — same semantics as today
- `window` (`trailing_12` | `calendar_year`) — governs `yearly_summary` AND `top_movers`

**Response shape:**

```python
class DashboardOverviewYearly(BaseModel):
    months: list[MonthValue]                           # trailing-N totals (existing)
    yearly_summary: YearlySummary                      # existing
    savings_rate_trend: list[SavingsRatePoint]         # NEW: 12 monthly points
    top_movers: TopMovers                              # NEW: { grew: [], shrank: [] }
    currency_mix: list[CurrencyMixPoint]               # NEW: 12 × {month, nis, usd}
    dividends: DividendsSummary | None                 # existing, augmented with trend_12mo
    taxes: TaxesSummary | None                         # existing, augmented with trend_12mo
    sources_health: list[SourceHealth]                 # existing
```

**Removed from the response** (these move to `/dashboard-monthly`):
- `current_month`, `current_month_*` (all hero fields), `current_month_top_categories`, `top_merchants_current_month`, `anomalies`.

**Removed from the params:** `month` is no longer accepted by this endpoint.

New nested types:

```python
class SavingsRatePoint(BaseModel):
    month: str           # "YYYY-MM"
    income_nis: float
    spending_nis: float
    savings_rate: float  # 0..1; (income − spending) / income, or 0 if income == 0

class CategoryDelta(BaseModel):
    slug: str
    label: str
    current_nis: float       # trailing-6 total (or trailing-12 if window=calendar_year)
    prior_nis: float         # prior-6 total
    delta_nis: float
    delta_pct: float | None  # None if prior was zero

class TopMovers(BaseModel):
    grew: list[CategoryDelta]    # top 5 by positive delta_nis
    shrank: list[CategoryDelta]  # top 5 by negative delta_nis (most-negative first)
    reason: str | None = None    # set to "insufficient_history" when prior period
                                 # is missing (e.g. calendar_year mode without a
                                 # full prior calendar year). Lists are empty in
                                 # that case and the UI shows an explainer.

class CurrencyMixPoint(BaseModel):
    month: str
    nis: float
    usd: float

class DividendsSummary(BaseModel):
    # existing fields …
    trend_12mo: list[MonthValue]  # NEW

class TaxesSummary(BaseModel):
    # existing fields …
    trend_12mo: list[MonthValue]  # NEW
```

### 5.2 `GET /api/expenses/dashboard-monthly` (new)

**Query params:**
- `user_id` (required)
- `month` (required, `YYYY-MM`)
- `fx` (`per_currency` | `nis`)

**Response shape:**

```python
class DashboardMonthly(BaseModel):
    month: str
    available_months: list[str]                        # for the MonthPicker
    chart_window: list[MonthValue]                     # 12 bars per the A-rule (below)
    hero_stats: HeroStatsMonthly                       # NEW shape
    top_categories: list[CategoryTotal]                # = current_month_top_categories
    categories_vs_typical: list[CategoryDeviation]     # NEW
    top_merchants: list[MerchantTotal]                 # = top_merchants_current_month
    largest_transactions: list[TransactionOut]         # NEW: top 5 by |amount_nis|
    anomalies: AnomalyBundle                           # = current anomalies
```

New nested types:

```python
class HeroMetric(BaseModel):
    value_nis: float
    mom_delta_pct: float | None       # vs prior month; None if no prior data
    vs_trailing12_pct: float | None   # vs trailing-12 avg; None if <3 prior months

class HeroStatsMonthly(BaseModel):
    spent: HeroMetric
    income: HeroMetric
    refunds: HeroMetric
    statements_reconciled: int        # no delta semantics
    anomalies_count: int              # no delta semantics

class CategoryDeviation(BaseModel):
    slug: str
    label: str
    this_month_nis: float
    typical_mean_nis: float           # trailing-12 mean for this category
    typical_std_nis: float            # floor at ₪50
    z_score: float                    # signed; positive = over typical
    delta_pct: float | None           # (this − mean) / mean
```

### 5.3 The A-rule (sliding-window chart_window)

Server-side, the Monthly endpoint computes 12 contiguous month buckets centered on `month` per the following deterministic algorithm:

1. Let `oldest` and `newest` = the oldest/newest months with any user data (per `tx.occurred_on`).
2. **Ideal centering:** `ideal_left = focal − 6`, `ideal_right = focal + 5`. (12 months total.)
3. **Slide at the past edge:** if `ideal_left < oldest`, shift the window right by `oldest − ideal_left` (so the new left bound = `oldest`).
4. **Slide at the future edge:** after step 3, if the (possibly-shifted) right bound > `newest`, shift the window left by `right − newest`, but never push left past `oldest`. So when both edges are tight (dataset shorter than 12 months), the window simply spans `[oldest, oldest+11]` regardless of focal.
5. **Generate 12 bars** for the resulting `[left, left+11]` range. For each bar:
   - If `bar_month < oldest` or `bar_month > newest` → `is_padding = True`, totals = 0.
   - If `bar_month == focal` → `is_selected = True`.

The `chart_window` response always has length 12. Each entry: `{month, total_nis, total_usd, is_padding: bool, is_selected: bool}`. Padding bars exist only when the user's full data history is shorter than 12 months.

### 5.4 Aggregation contracts (deterministic; no LLM calls)

All new aggregations:
- Run from `expense_transactions` table (or join to `expense_categories` for category labels).
- Filter to `is_inflow=False AND is_excluded_from_spend=False AND direction='debit'` for spending series, mirror filter for income/refund series.
- Use `tx.occurred_on` not statement boundaries (the same fix that powered the Discount per-month timeline correction in EX4.x).
- Are compatible with the FX mode: `fx=per_currency` returns NIS and USD totals separately; `fx=nis` returns NIS-only (USD column zeroed), since server-side conversion is still TBD.

**Numeric edge cases:**
- `mom_delta_pct` returns `None` when prior month spending is exactly 0 (division-by-zero guard).
- `vs_trailing12_pct` returns `None` when fewer than 3 prior months exist OR the trailing-12 average is exactly 0.
- `savings_rate` returns 0 when income is 0 (no infinity, no NaN).
- `delta_pct` in `CategoryDelta` returns `None` when prior is 0; the delta_nis is still populated.
- `categories_vs_typical` excludes any category with fewer than 3 months of prior data (per §7.3) AND uses a stddev floor of ₪50 to avoid z-score blowups.

## 6. Frontend changes

### 6.1 Routes

- `ui/src/app/expenses/layout.tsx` — add `Monthly` to `TABS` array, slot 2.
- `ui/src/app/expenses/page.tsx` — strip month-rescope logic; consume new `dashboardOverview` shape; render Overview-only widgets per §4.2.
- `ui/src/app/expenses/monthly/page.tsx` — **new file**; mirrors today's monthly behaviors but consumes `dashboardMonthly` payload; renders Monthly widgets per §4.3.

### 6.2 New components

All under `ui/src/components/expenses/`:

- `savings-rate-trend.tsx` — line/area Recharts component. Tooltip shows `(income, spending, rate%)` per month.
- `top-movers-card.tsx` — paired horizontal bar chart (or two stacked bar chart sections). Categories click through to `/expenses/transactions?category=<slug>`.
- `currency-mix-card.tsx` — stacked bar chart, 12 months × NIS/USD.
- `categories-vs-typical-card.tsx` — table-of-rows with mini-bars showing this-month vs typical band.
- `largest-transactions-card.tsx` — table; each row click navigates to transactions tab with that tx highlighted.

### 6.3 Modified components

- `hero-stats.tsx` — add `mode: 'yearly' | 'monthly'` prop. Yearly mode hides MoM annotations and reads from `dashboardOverview.yearly_summary` totals. Monthly mode reads from `dashboardMonthly.hero_stats` and renders MoM/trailing-12 deltas with arrow icons.
- `monthly-spend-chart.tsx` — add `mode: 'small' | 'focal'` prop. `small`: short height, no MonthPicker integration, click navigates via `useRouter().push('/expenses/monthly?month=...')`. `focal`: tall, click calls `onMonthSelected` (rescope-in-place), highlights `is_selected` bar, fades `is_padding` bars.
- `dividends-card.tsx` / `taxes-card.tsx` — accept optional `trend12mo: MonthValue[]`; render inline mini-bar chart when present.

### 6.4 API client

`ui/src/lib/expenses/api.ts`:
- Update `dashboardOverview()` return type and signature (remove `month` param, add typed fields for `savings_rate_trend`, `top_movers`, `currency_mix`).
- Add `dashboardMonthly(userId, month, fxMode)` returning `DashboardMonthly`.

## 7. Backend changes

### 7.1 New endpoint module

`argosy/api/routes/expenses.py` — extend with `dashboard_monthly()` handler. Register on the existing expenses router.

### 7.2 New aggregation helpers

Today's `dashboard_overview` is one ~250-line function inline in `argosy/api/routes/expenses.py:642+` with all aggregations expressed as inline SQL — there is no service-layer module yet. As part of this wave we extract aggregation logic into a new module `argosy/services/expense_dashboard.py`. The route handlers (`dashboard_overview`, `dashboard_monthly`) become thin orchestrators that compose helper calls.

New helper functions (all in `argosy/services/expense_dashboard.py`, all sync, all session-driven, no LLM calls):

- `compute_savings_rate_trend(session, user_id, months) -> list[SavingsRatePoint]`
- `compute_top_movers(session, user_id, window) -> TopMovers`
- `compute_currency_mix(session, user_id, months) -> list[CurrencyMixPoint]`
- `compute_dividends_trend_12mo(session, user_id) -> list[MonthValue]`
- `compute_taxes_trend_12mo(session, user_id) -> list[MonthValue]`
- `compute_chart_window(session, user_id, focal_month) -> list[ChartWindowBar]`
- `compute_hero_stats_monthly(session, user_id, month) -> HeroStatsMonthly`
- `compute_categories_vs_typical(session, user_id, month) -> list[CategoryDeviation]`
- `compute_largest_transactions(session, user_id, month, limit=5) -> list[TransactionOut]`

Each helper is independently testable with deterministic SQL.

### 7.3 Z-score floor

`compute_categories_vs_typical` floors stddev at ₪50 to avoid divide-by-near-zero blowups for sparse categories. Categories with fewer than 3 prior months of data are excluded from the result.

## 8. Testing

### 8.1 Unit tests (Python, `tests/`)

- `test_compute_savings_rate_trend.py` — fixture: synthetic transactions across 14 months with known income/spending; assert per-month rate.
- `test_compute_top_movers.py` — fixture with categories that grew, shrank, and stayed flat; assert top-5 grew/shrank.
- `test_compute_currency_mix.py` — fixture with NIS-only, USD-only, and mixed months; assert per-currency split.
- `test_compute_chart_window.py` — covers all four edge cases: ideal centering, slide-right at past edge, slide-left at future edge, dataset-shorter-than-12.
- `test_compute_hero_stats_monthly.py` — assert MoM delta = 0 for first month, correct for subsequent; trailing-12 delta None until 3 prior months exist.
- `test_compute_categories_vs_typical.py` — assert z-score sign + magnitude; assert sparse categories filtered out.
- `test_compute_largest_transactions.py` — order by `|amount_nis|` desc; respect filters.

### 8.2 API tests (Python)

- `test_dashboard_overview_shape.py` — reshaped endpoint returns new fields and no longer returns `current_month_*` / `anomalies`.
- `test_dashboard_monthly_shape.py` — new endpoint returns 12-bar chart_window with one `is_selected=True` bar; hero_stats shape is correct; `largest_transactions` is length ≤ 5.
- `test_dashboard_monthly_padding.py` — when user has only 4 months of data, response still has 12 bars, 8 of them `is_padding=True`.

### 8.3 Frontend tests

- Component tests for each new component (`savings-rate-trend`, `top-movers-card`, `currency-mix-card`, `categories-vs-typical-card`, `largest-transactions-card`) rendering with sample fixture data.
- One e2e click-flow: load `/expenses` → assert Overview content → click a bar in the small chart → assert URL became `/expenses/monthly?month=YYYY-MM` → assert MonthPicker reflects the chosen month.

### 8.4 Manual UI smokes

Per binding policy ("manual UI smokes deliberately skipped"), no manual-smoke gate in the plan. Backend tests + the one e2e cover the integration risk.

## 9. Migration / data layer

**None.** No schema changes, no Alembic migration. All data exists in `expense_transactions` + the existing aggregations the backend already runs for `dashboard-overview`.

## 10. Risks and open questions

- **`window=calendar_year` + `top_movers`:** trailing-6-vs-prior-6 doesn't translate cleanly to a calendar-year mode. **Decision:** when `window=calendar_year`, `top_movers` compares the current calendar year so far vs the prior calendar year through the same end month. If we don't have a full prior calendar year, return an empty `grew`/`shrank` with a `reason: "insufficient_history"` flag (UI shows an explainer card).
- **MoM delta for the first month of a user's data:** `mom_delta_pct = None`; `vs_trailing12_pct = None` until ≥3 prior months. UI shows "—" instead of arrow.
- **`is_padding` bars in chart_window:** UI must visually fade these (e.g. 30% opacity). Tooltip suppresses values for padding bars.
- **Currency-mix at `fx=nis`:** since server-side FX conversion is still TBD (open item §16-5), the chart degrades gracefully — `fx=nis` mode shows NIS-only bars and a small note "USD spending hidden until FX conversion is wired".
- **Bundle size:** five new chart components on Recharts. Recharts is already loaded; incremental cost is small. No code-split needed for v1.

## 11. Phasing

Single coherent implementation; no sub-waves. Implementation plan (separate doc) will sequence:

1. Backend aggregation helpers + unit tests (parallelizable).
2. Reshape `/dashboard-overview` + new `/dashboard-monthly` endpoint + API tests.
3. New frontend components (parallelizable) + component tests.
4. New `/expenses/monthly` route + tab nav update.
5. Reshape `/expenses` (Overview) page to consume new payload.
6. E2e click-flow test.
7. Verify against current dev DB; restart uvicorn + Next.js; eyeball smoke (informal).

## 12. Done criteria

- `pytest -m "not llm_eval"` green.
- `npm run lint` + `npm run typecheck` green in `ui/`.
- Loading `/expenses` shows yearly view with all four new widgets.
- Loading `/expenses/monthly` shows monthly view with all three new widgets + MoM deltas on hero.
- Clicking a bar on Overview's small chart navigates to Monthly with that month selected.
- Clicking a bar on Monthly's focal chart re-scopes Monthly without navigating.
- All existing `/expenses` features still work (transactions, sources, trips, RSU, FX toggle, window toggle).
- SDD §16 handover note updated with EX6 wave summary.

---

## Appendix A — Widget-to-data mapping (cheat sheet)

| Widget | Tab | Data source | Endpoint field |
|---|---|---|---|
| `HeroStats (yearly)` | Overview | `yearly_summary` totals | `dashboard-overview.yearly_summary` |
| `YearlySummaryCard` | Overview | existing | `dashboard-overview.yearly_summary` |
| `SavingsRateTrend` | Overview | new agg | `dashboard-overview.savings_rate_trend` |
| `TopMoversCard` | Overview | new agg | `dashboard-overview.top_movers` |
| `CurrencyMixCard` | Overview | new agg | `dashboard-overview.currency_mix` |
| `DividendsCard` (augmented) | Overview | existing + new trend | `dashboard-overview.dividends.trend_12mo` |
| `TaxesCard` (augmented) | Overview | existing + new trend | `dashboard-overview.taxes.trend_12mo` |
| `MonthlySpendChart (small)` | Overview | existing | `dashboard-overview.months` |
| `SourcesHealthTable` | Overview | existing | `dashboard-overview.sources_health` |
| `MonthPicker` | Monthly | new | `dashboard-monthly.available_months` |
| `MonthlySpendChart (focal)` | Monthly | new agg | `dashboard-monthly.chart_window` |
| `HeroStats (monthly+MoM)` | Monthly | new agg | `dashboard-monthly.hero_stats` |
| `CategoryDonut` | Monthly | existing | `dashboard-monthly.top_categories` |
| `CategoriesVsTypicalCard` | Monthly | new agg | `dashboard-monthly.categories_vs_typical` |
| `TopMerchantsCard` | Monthly | existing | `dashboard-monthly.top_merchants` |
| `LargestTransactionsCard` | Monthly | new agg | `dashboard-monthly.largest_transactions` |
| `AnomalyHighlights` | Monthly | existing | `dashboard-monthly.anomalies` |
