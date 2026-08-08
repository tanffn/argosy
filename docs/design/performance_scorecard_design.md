# Design — Portfolio Performance Scorecard (TWR vs benchmark + attribution)

**Status:** DESIGN ONLY — not implemented. Written for independent review.
**Author:** Claude (Argosy reviewer session), 2026-08-07.
**Purpose:** Give Argosy the one instrument it lacks for its core goal ("maximize
the family's wealth"): a measured answer to *"are we beating the market, and
where is value added or lost?"* Today this is a narrative, not a number — there
is **no realized-return or benchmark-comparison logic anywhere** (verified).

---

## REVISIONS — adversarial review 2026-08-08 (BINDING; apply before build)

An independent adversarial review (verified against the live DB) found one
**critical** flaw in this design. These amendments override the sections they name.

- **[CRITICAL] §2.2 must NOT auto-classify a position going to zero shares as a
  "flow".** The share/price decomposition treats `(shares_t1−shares_t0)×price` as
  an excluded external flow. But a *silent deletion* (the July book erasure — 16
  positions → 0 shares) is arithmetically identical to a *withdrawal*, so the
  scorecard would exclude the erasure and report a **pristine TWR straight through
  a $2.7M loss** — silent success reborn inside the tool built to catch it.
  **Amendment:** any position dropping to zero shares (or an entire account
  disappearing) is a **candidate corruption event** — it must be reconciled
  against a real sale/vest (proposals `executed_live` / `rsu_vest_events`) or
  **quarantined and the period flagged fail-loud**, never silently classified as
  flow. A sub-period containing an unreconciled zero-out does not produce a return
  number.
- **[CRITICAL] Integrity floor is an INPUT CONTRACT, not an assumption.** C must
  read a per-snapshot integrity verdict (the conservation gate) and **refuse /
  quarantine, fail-loud,** any period whose snapshot did not pass. C never
  computes a return on a book that failed conservation.
- **[HIGH] Reconcile with the ledger (Component B) or fail loud.** C's Brinson
  selection effect and B's per-bet grading are two truth systems over the same
  trades with different windows/prices. They must be reconciled to a tolerance;
  divergence beyond it is a fail-loud flag, not two coexisting numbers (else the
  "which number is right?" ambiguity returns).

---

Reviewers: §2 (methodology + the *why* of each choice) and §3 (data foundation
with exact tables/columns + gaps) are the load-bearing sections. Every design
choice is justified so you can challenge the reasoning, not just the code.

---

## 1. Goal & scope

Answer three questions, refreshed each snapshot, in the family's spending
currency (NIS) and in USD:

1. **Return** — what did the portfolio actually earn over period X? (realized,
   flow-adjusted — not the assumed forward `real_return` in `fi_crossing.py`.)
2. **Vs the alternatives** — did we beat (a) the market and (b) our *own*
   strategy indexed passively?
3. **Attribution** — *where* did return come from: allocation, selection, cash
   drag, FX, tax?

Out of scope: forward projection (already covered by `cashflow_projection.py` /
retirement MC) and prediction-accuracy (already `predictions/evaluator.py`).
This is **realized ex-post** measurement only.

---

## 2. Methodology — and why each choice

### 2.1 Time-Weighted Return (TWR) as the headline — *why not IRR/MWR*
- **TWR** neutralizes the size and timing of external cashflows (RSU vests,
  deposits, withdrawals), which the *investor* controls, not the *strategy*. It
  is the correct lens for "did the strategy beat the market" and is what any
  benchmark comparison must use. A $50k RSU vest must never read as a "+3% gain."
- We **also** compute **Money-Weighted Return (IRR)** separately, because it
  answers a different, legitimate question — the family's *actual dollar
  experience* including when they added cash. Report both; never conflate them.
  Headline skill number = TWR vs benchmark; personal-outcome number = IRR.

### 2.2 The cashflow problem, and the unlock — *position-level share/price decomposition*
A correct TWR needs to separate **market return** from **external flows**. The
data inventory (§3.2) is blunt: there is **no clean dated cashflow ledger**
(`fills`/`lots`/`daily_account_pnl` are empty). Reconstructing every buy/sell
from undated `proposals` + `closed_loop` prose would be fragile.

**The unlock:** `portfolio_snapshots.positions_json` stores per-position
**`shares` AND `current_price`**. So between two snapshots we can decompose each
position's value change deterministically:

```
Δvalue = (price_t1 − price_t0) × shares_held   ← MARKET RETURN (what we measure)
       + (shares_t1 − shares_t0) × price_t1     ← FLOW (buy/sell/vest — excluded)
```

This turns the missing-ledger blocker into a solved problem for **security
holdings**: market return is computed directly from prices on shares actually
held, and share deltas are treated as flows. We do **not** need a fill ledger to
get security-level TWR. (Cross-check: reconstructed flows should reconcile with
`rsu_vest_events` for NVDA-family inflows and with `proposals(status=
executed_live)` for the 9 known trades — a validation, not a dependency.)

- **Method:** **Modified Dietz** per sub-period (snapshot t0→t1), where flows are
  the share-delta cashflows above, time-weighted within the period. With
  snapshot boundaries at each import, flows sit ≈ at the boundary, so Modified
  Dietz ≈ true TWR; we flag any sub-period with a large mid-period flow as
  lower-confidence. Link sub-period returns geometrically → cumulative TWR.
  *Why Modified Dietz:* it is the accepted approximation when you have
  period-boundary market values + flows but not a daily NAV — exactly our case.

### 2.3 Cash and non-security assets
- **Cash** (`totals_json.cash_balances_usd_k`, plus cash positions): its balance
  changes from (a) external deposits/withdrawals — flows to exclude — and (b)
  interest/T-bill yield — return to include. External cash flows are
  reconstructed from `rsu_vest_events` (dated, clean) and, where a cash-balance
  change is not explained by a security flow or known vest, flagged as an
  **unattributed flow** (confidence-scored, surfaced honestly — see §5).
- **Pensions / real estate** (`pensions_json`, `real_estate_json`): tracked in a
  SEPARATE bucket. They are illiquid, valued sporadically, and not part of the
  "beat the market with our investing" question. The headline scorecard is the
  **liquid investable portfolio**; total-net-worth return is a secondary readout.
  *Why:* mixing a rarely-repriced apartment into TWR pollutes the equity-skill
  signal. Keep them separated and labelled.

### 2.4 Two benchmarks — *why both*
- **Market benchmark** — ACWI (or VT), global equity. Answers "did we beat just
  buying the world?" But beating it can be pure *beta* (more equity, more risk).
- **Policy benchmark** — the plan's **own** `target_allocation_json`: each sleeve
  → a representative index ETF, weighted by `target_pct`, rebalanced to target.
  Answers "did our *active decisions* beat our *own strategy* held passively?"
  *Why this is the important one:* it isolates skill from beta. Given we already
  measured **38.6% long hit-rate** (no selection edge), the policy benchmark is
  what will show whether our stock-picking is *costing* us vs just indexing each
  sleeve. This is the number that most directly serves "maximize wealth."

### 2.5 Attribution — *why Brinson*
Decompose **active return (portfolio − policy benchmark)** into:
- **Allocation effect** — over/under-weighting a sleeve vs its `target_pct`,
  times the sleeve benchmark return.
- **Selection effect** — within-sleeve instrument returns vs the sleeve's index.
- **Cash drag** — cash weight × (equity benchmark return − cash yield).
- **FX effect** — NIS-basis return minus USD-basis return (see §2.6).
- **(Tax** noted qualitatively; realized-tax attribution deferred — no realized
  dated sale ledger yet, §3.2.)

*Why Brinson:* it maps one-to-one onto the actionable levers from the
maximize-wealth plan (allocation / selection / cash / FX). It tells us not just
*if* but *where* — e.g. "selection cost 2.1%, allocation added 0.6%, cash drag
−0.3%." Sleeve mapping is the strongest part of the foundation (§3.4).

### 2.6 Currency — *why NIS base with FX isolated*
The family retires in ILS, so return must be reported in **NIS** (their spending
currency) using the `fx_rates` daily series (§3.2). We compute the SAME return in
**USD** too; **FX effect = NIS return − USD return**. *Why:* the book is
USD-heavy; a strong quarter in USD can be a flat quarter in NIS purely on FX. FX
is a real P&L driver and must be a named line, never silently baked in.

### 2.7 Risk-adjusted — *why the Information Ratio is the headline skill metric*
From the linked sub-period return series: annualized return, volatility,
**max drawdown**, **Sharpe** (vs an ILS risk-free from BoI/T-bill), **tracking
error** vs policy benchmark, and **Information Ratio = active return / tracking
error**. *Why IR:* return alone rewards taking more risk. IR is
active-return-per-unit-of-active-risk — the single cleanest "are we actually
skilled, or just louder?" number. Beating the market with 2× the volatility is
not skill.

---

## 3. Data foundation (grounded — exact tables/columns) + gaps

### 3.1 Value series — `portfolio_snapshots` (`models.py:518`)
Columns: `snapshot_date, imported_at, positions_json, allocations_json,
nvda_sales_json, real_estate_json, pensions_json, totals_json, fx_usd_nis,
fx_usd_eur`. `positions_json[]` carries `symbol, shares, current_price,
current_value_local, usd_value_k, currency` — **this is what enables §2.2**.
`totals_json = {total_usd_value_k, cash_balances_usd_k}`.
- **48 rows, 2026-03-24 → 2026-08-07.** IRREGULAR: one March point, a hole to
  2026-06-12, then dense (multiple rows/day). Reads are latest-only
  (`ORDER BY imported_at DESC`). **GAP #1:** must dedup to one canonical
  period-end per date and accept an irregular, ~4.5-month, hole-bearing series.

### 3.2 External flows — the make-or-break gap
- **`rsu_vest_events`** — 80 rows, `vest_date` 2022-06-15 → 2026-06-17,
  `shares_net, fmv_per_share_usd`. The ONLY clean dated external inflow (June-2026
  rows are `source='derived:...'` reconstructions). **USABLE.**
- **`fx_rates`** — 459 rows USD/NIS daily 2024-10 → 2026-08. **USABLE** for §2.6.
- **`proposals`** — 18 rows (9 `executed_live`) but **no fill price/timestamp**
  (only `created_at/updated_at`). Cross-check only.
- **`fills` / `lots` / `daily_account_pnl` / `investor_events` — 0 rows (EMPTY).**
- `closed_loop.py` parses fills from TSV prose; applied via one-off scripts, not
  persisted. `nvda_sales_json` is **month-label only** (no year/date) — unusable
  as a flow.
- **GAP #2 (primary risk):** no clean dated cashflow ledger. Mitigation: §2.2
  derives security flows from snapshot share-diffs (no ledger needed);
  `rsu_vest_events` covers RSU inflows; residual cash moves are flagged
  unattributed. The design is feasible *because* §2.2 sidesteps the ledger — but
  the reviewer must scrutinize the share-diff flow logic and the
  unattributed-cash handling. This is where the design most needs adversarial
  review.

### 3.3 Benchmark prices
`yfinance_adapter.get_eod_prices(tickers, start, end)` (`:224`) — live fetch into
a TTL `kv_cache` (6h). No durable price-history table. **GAP #3:** need a
persisted `benchmark_prices` table (index/ETF closes aligned to snapshot dates)
so the scorecard is reproducible and not dependent on live yfinance every load.

### 3.4 Sleeve mapping + target weights — SOLID
- `instrument_plan_classes` (44 rows) + `resolve_sleeve_label(...)`
  (`instrument_plan_class.py:149`) → canonical sleeve per symbol (owner>fleet>plan).
- `plan_versions.target_allocation_json` → `TargetAllocationDoc`
  (`target_allocation_doc.py:93`): `classes[].{label, target_pct, instruments[]}`.
  89 `plan_versions` rows give target-history over time.
- **USABLE** — the attribution mapping layer is real; per-sleeve benchmark ETFs
  can be drawn from `classes[].instruments[].symbol` or a curated sleeve→ETF map.

### 3.5 Adjacent (reuse, don't duplicate)
- `net_worth_backfill.py` (`/api/portfolio/net-worth-history`) reconstructs
  net-worth points from archived TSVs, **dual USD/NIS**. **Reuse** to BACKFILL
  the value series before 2026-06 and extend history (partly closes GAP #1).
- `wealth_dashboard.py`, `target_progress.py` — value/target surfaces to sit
  the scorecard beside; not return logic.

---

## 4. Computation pipeline (what the build would do — not built)

1. **Canonical value series:** dedup `portfolio_snapshots` to one period-end per
   date; backfill pre-June via `net_worth_backfill` archived TSVs. Split into
   liquid-investable vs pension/real-estate buckets.
2. **Flow derivation:** per consecutive pair, per position, split Δvalue into
   market-return vs share-flow (§2.2); reconcile flows against `rsu_vest_events`
   + `executed_live` proposals; residual cash-balance change → yield vs
   unattributed flow (confidence-scored).
3. **Sub-period returns:** Modified Dietz per pair, USD and NIS (`fx_rates`);
   geometric link → cumulative TWR; also solve IRR (MWR) on the flow set.
4. **Benchmarks:** fetch + PERSIST ACWI/VT and per-sleeve ETF closes to
   `benchmark_prices`; build the policy-index return from `target_allocation_json`
   weights; rebalance to target at each period.
5. **Attribution:** Brinson allocation/selection + cash drag + FX (§2.5–2.6).
6. **Risk-adjusted:** vol, maxDD, Sharpe, tracking error, IR (§2.7).
7. **Persist** to a derived `portfolio_return_periods` (+ summary) cache keyed on
   snapshot/plan version (mirror `derived_cache` versioning); expose
   `GET /api/portfolio/performance`; UI `/performance` card.

New tables: `benchmark_prices` (durable), `portfolio_return_periods` (derived
cache). No changes to existing ingest.

---

## 5. Honesty / confidence (must ship WITH the number)
- **Short, holed, thin history** (~4.5 months, March→June gap, single-user). The
  headline must carry a **confidence band + N periods**; results are provisional
  and firm up as history accrues. Do NOT annualize a 4.5-month return without a
  loud caveat.
- **Reconstructed flows are approximate.** Every unattributed cash move and every
  large mid-period flow is flagged; the scorecard shows a data-confidence score
  per period, not a false-precision single number. (This is Argosy's output-trust
  doctrine: every number auditable to raw rows.)
- **Benchmark choice is a judgment** (ACWI vs VT vs a NIS-hedged blend) — make it
  explicit and configurable; show which was used.

---

## 6. Phased build (each phase independently shippable)
- **0a — Headline (MVP):** dedup series → position-decomposition TWR (USD+NIS) vs
  ACWI, with confidence band. Ships "are we beating the market?" as a number.
  Data: existing snapshots + new `benchmark_prices`. Lowest risk.
- **0b — Policy benchmark + Brinson attribution** (allocation/selection/cash/FX)
  via the sleeve map. This is the "is our stock-picking costing us?" answer.
- **0c — Risk-adjusted + history backfill** (net_worth_backfill) + durable
  `portfolio_return_periods` + `/performance` API/UI + IRR.

---

## 7. Open questions for the reviewer
1. **Share-diff flow logic (§2.2):** is decomposing Δvalue into
   price-return vs share-flow sound given snapshots can carry stale/re-imported
   prices and multiple rows per date? Where could it double-count or misattribute
   a corporate action (split/spin — e.g. the NOW 5:1-class events)?
2. **Unattributed cash (§3.2):** is confidence-flagging honest enough, or must we
   build the real cashflow ledger (persist `fills`) before claiming TWR at all?
3. **Base currency:** NIS headline with USD secondary — agreed? Or NIS-hedged
   policy benchmark to strip FX from the *skill* comparison?
4. **Benchmark selection:** ACWI vs VT vs a custom blend matching the plan's
   geographic mix — which is the fair "market"?
5. **Pension/RE separation (§2.3):** correct to exclude from headline TWR, or does
   the family want a single total-wealth return despite the repricing noise?
6. **History depth:** is a 4.5-month provisional number worth shipping now, or
   backfill first (0c before 0a) so the first number shown isn't noise?
