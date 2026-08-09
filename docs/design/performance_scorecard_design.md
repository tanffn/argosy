# Design — Portfolio Performance Scorecard (TWR vs benchmark + attribution)

**Status:** DESIGN ONLY — not implemented. Written for independent review.
**Author:** Claude (Argosy reviewer session), 2026-08-07.
**Purpose:** Give Argosy the one instrument it lacks for its core goal ("maximize
the family's wealth"): a measured answer to *"are we beating the market, and
where is value added or lost?"* Today this is a narrative, not a number — there
is **no realized-return or benchmark-comparison logic anywhere** (verified).

**Input contract:** this scorecard is Component C of the Argosy operating model
(`docs/design/argosy_operating_model_spec.md`). Any **headline / proof** return
number it publishes reads **only** `validated_snapshot_period` spine records over
per-item-bound `validated_snapshot` records (that spec §2A) — never raw
`portfolio_snapshots` directly. A spine period exists only if its snapshots passed
the conservation gate **and** their per-item integrity binding verified **and** each
normalized item was bound to an independent source record with expected-set
completeness proven (spec §2A `item_source_binding` / `expected_set_completeness` —
the fix for ingest-time sub-threshold corruption the Merkle binding alone cannot
catch), its flows reconciled to dated provenance IDs, and its prices are fresh
(below). **A missing
required field or a failed integrity verdict yields NO period and NO return number
— never a "degraded confidence" figure.** A pre-spine computation is permitted
**only** as an explicitly-labelled non-headline diagnostic (§6 0a) that may not
feed learning or any "are we beating the market?" proof surface.

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
       + (shares_t1 − shares_t0) × price_t1     ← SHARE-DELTA NOTIONAL (see caveat)
```

**The share-delta term is NOT an "external flow," and treating it as one is a second
error the earlier draft made.** `(shares_t1 − shares_t0) × price_t1` is the notional
of whatever changed the share count. A TWR external flow is only a **deposit,
withdrawal, or vest** — cash the *investor* moved in or out. An **internal buy or
sell** (rotating between two held securities) moves no cash into or out of the
portfolio and is **not** an external TWR flow at all; excluding its notional as if it
were double-counts. Worse, endpoint decomposition cannot measure return on a share
**held for only part of the period**: buy 1 share for $100 at mid-period, end at $110,
and the true position return is +10% with **zero** external flow — yet
`(shares_t1−shares_t0)×price_t1 = $110` gets booked as an excluded flow and the
mid-period gain is misclassified. The endpoints simply do not carry the information.

This decomposition is a useful **diagnostic**, but it is **not, by itself, proof**
of return, and the previous draft over-claimed when it called it "a solved problem"
that needs "no fill ledger." Two things are false as stated:

- **Snapshot endpoints do not pin down what happened between them.** The pair
  (shares_t0·price_t0, shares_t1·price_t1) is *identical* whether a share was bought
  at $100 near t0 or at $110 just before t1. Endpoints alone cannot distinguish
  market return earned on shares *held through* the period from return on shares
  *traded within* it — so the market-return / flow split is an assumption unless the
  intra-period flows are dated.
- **A `proposals` row is not fill evidence.** It has no fill price, no fill
  timestamp, and no binding of quantity to a specific share delta. A stale or
  mismatched proposal can therefore "bless" a corrupted deletion as a legitimate
  sale. `proposals(status=executed_live)` is a weak cross-check, **never**
  sale-provenance for a delta.

Therefore: **proof-quality TWR requires machine-verifiable, dated flow provenance —
dated fill/vest/transfer/corporate-action records (amount + price) for every
share/cash delta, or broker-authored NAV/transaction data.** This is exactly what
`validated_snapshot_period.flow_reconciliation_status` (spec §2A) now requires: a
delta without a dated provenance ID is unreconciled and the period is quarantined.
**Without dated flows or broker NAV, a computed period is a diagnostic, not proof,
and is labelled as such — never a headline "return."**

**Dated provenance of the OBSERVED deltas is still not enough — the intra-period
EVENT SET must be provably complete (`expected_event_set_completeness`, spec §2A).**
Flow provenance dates every delta *visible in the endpoints*; it cannot see an event
that leaves the endpoints unchanged. **NET-ZERO round-trips are the motivating
failure:** a position **sold then repurchased within the same period** (offsetting
cash) leaves identical begin/end shares and identical cash, so every
endpoint / share-diff / provenance check passes while the scorecard wrongly treats
the name as **continuously held** — silently corrupting the effective holding window
and therefore the §2.5 selection attribution on a period that otherwise looks
proof-valid. This is the period analogue of the position-level
`expected_set_completeness`. Therefore a **proof-grade** period requires an
**INDEPENDENT broker activity/transaction manifest** (or broker-authored
NAV/activity data) attesting the *complete set* of intra-period events — every buy,
sell, vest, transfer, and corporate action — not merely dated provenance for the
observed net deltas. **Even after `fills` are populated, a period without event-set
completeness is DIAGNOSTIC, not proof:** populated fills date the deltas we *saw*,
but only a broker-authored event manifest proves there were no *other* events (the
net-zero round-trip) we never saw. C reads
`validated_snapshot_period.expected_event_set_completeness` and refuses to publish a
headline return for any period whose event set is not independently closed. (This is still narrower than
"no cost data is needed anywhere": the operating model's **tax-aware** grading —
HOLD-vs-alternative and A2 switch-now, spec §5 B2 / §4 A2 — is *separately and
fatally* blocked on lot-level cost basis. TWR measures return; it licenses no
after-tax switch or grade.)

**The decomposition is dangerous, and here is the guard.** Reading a share
delta as an "excluded flow" is exactly how a *silent corruption* launders into a
pristine return: `(shares_t1−shares_t0)×price` for a position that dropped to zero
is arithmetically identical to a clean withdrawal, so a naive scorecard would
exclude the July book erasure (16 positions → 0 shares) and report a **pristine
TWR straight through a $2.7M loss** — silent success reborn inside the tool built
to catch it. A full deletion is only the loudest case; the same laundering happens
on partial damage and bad prices. Therefore, before any sub-period return is
computed, the period must pass these **fail-loud reconciliation gates** — a
failure QUARANTINES the sub-period (no return number), it is **never** downgraded
to a "confidence warning":

- **Full zero-out / account disappearance** — a position going to zero shares, or
  an entire account vanishing, is a **candidate corruption event**. It must be
  reconciled to a **dated sale/vest/transfer provenance record** (a fill or
  `rsu_vest_events` row carrying date + amount + price; a bare
  `proposals.executed_live` row is **not** sufficient — it has no fill price,
  timestamp, or quantity binding and could bless a corrupted deletion) or the period
  is quarantined fail-loud. Never auto-classified as flow.
- **Partial drop (NEW — the amendment the old banner missed).** A drop of **more
  than X% of positions or of total value** between snapshots that is *not*
  reconciled to executed sales/withdrawals is a candidate corruption event and
  **fails loud**, exactly like a full zero-out. The prior "zero-shares" guard only
  caught total deletions; a 40% silent shrink would still have laundered through as
  a flow. Partial damage now fails loud too.
- **Stale prices.** If any `current_price` in the pair is older than the freshness
  threshold (a re-import that carried forward stale marks), the sub-period **fails
  loud** — a return computed on stale prices is fiction, not low-confidence.
- **Split / spinoff / share-count change from a corporate action.** A share count
  that changed because of a split, reverse-split, or spinoff (not a trade) will
  read as a giant spurious flow. Such events must be detected and the shares
  **adjusted to a common basis** before decomposition, or the period **fails
  loud** — never silently absorbed as a flow (the NOW 5:1-class events).
- **Internal transfer misread as a buy/sell (NEW).** A position moving *between the
  family's own accounts* (an in-kind transfer, an ACATS, a re-registration) nets to
  zero at book level but at the per-position/per-account grain looks like a clean
  sell in one account and a clean buy in another — i.e. two mislabeled "flows" that
  never touch the market and were never trades. The decomposition would exclude both
  as external flows and quietly reshape the return. A share-delta that reconciles to
  **neither** a dated executed sale/withdrawal fill **nor** a vest
  (`rsu_vest_events`) — yet is offset by an equal-and-opposite share-delta in another
  account of the same book — is a candidate internal transfer: it must be **matched
  and netted to zero flow across accounts** (ideally against a dated transfer/ACATS
  provenance ID), or the period **fails loud**. It is never booked as an external
  flow on the strength of the single-account view.

The `validated_snapshot_period.flow_reconciliation_status` and `price_freshness`
fields (operating-model spec §2A) are exactly these verdicts, and
`flow_reconciliation_status` requires a **dated provenance ID for every share/cash
delta** (not just "reconciled to a proposal"); C reads them and refuses any period
that did not pass. **A period with an unreconciled zero-out, an unexplained partial
drop, stale prices, an unadjusted corporate action, or any delta lacking dated
provenance does not produce a return number.**

- **Method (proof path):** **segment holdings at dated events, then Modified Dietz on
  external flows only.** Concretely: (1) using the dated fill/vest/transfer provenance
  IDs (`validated_snapshot_period.flow_reconciliation_status`, spec §2A), cut each
  position's holding into **effective sub-windows** bounded by its actual dated
  events, and compute a **position-period return over each ACTUAL effective window** —
  a share held only part of the period is returned only over the days it was held, so
  the mid-period-buy case above is measured correctly instead of misbooked. (2) Feed
  Modified Dietz **only genuine external contributions/withdrawals** (deposits,
  withdrawals, vests — dated, time-weighted at their real dates), **never internal
  trade notionals** (an internal buy/sell is not an external flow). Link sub-period
  returns geometrically → cumulative TWR. *Why Modified Dietz:* it is the accepted
  approximation when you have period-boundary market values **and dated flows** but
  not a daily NAV — which is our case **only once flows carry dated provenance.**
- **Without dated fills there is no proof path.** Snapshot endpoints alone do not date
  flows, cannot segment part-period holdings, and cannot separate an internal rotation
  from an external flow. A period reconstructed from endpoints only is therefore a
  **diagnostic, not a proof number** (§6 0a) — consistent with the §2.2 proof gate and
  the input contract; it is **not** silently treated as boundary-dated, because an
  undated intra-period flow can move the true return materially.

### 2.3 Cash and non-security assets
- **Cash** (`totals_json.cash_balances_usd_k`, plus cash positions): its balance
  changes from (a) external deposits/withdrawals — flows to exclude — and (b)
  interest/T-bill yield — return to include. External cash flows are reconstructed
  from `rsu_vest_events` (dated, clean) and matching dated fill/transfer provenance.
  **An unexplained cash movement is NOT laundered through a confidence score.** The
  spine state is binary (a period is proof-valid or it is not), and a
  "confidence-scored unattributed flow" would let repeated sub-threshold cash
  understatements accumulate to a material error while each period still looked fine.
  Therefore an unexplained cash change is retained as an **UNCLASSIFIED observation**
  and **blocks the proof/headline return for that period** unless independent
  evidence (a dated deposit/withdrawal/interest record) classifies it. There is no
  "small residual → silent pass." A period whose cash change is not fully classified
  yields a diagnostic, not a proof number (see §5).
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

**Reconciliation with the ledger (Component B) — a gate, not a hope, and not a
naive sum.** C's Brinson **selection effect** and Component B's per-bet grading
(operating-model spec §5) are two truth systems over the same book. "They use the
same benchmark, so they agree" is false. The previous draft's fix — *defining* C's
aggregate selection effect as **equal to the sum of B's per-bet vs-benchmark
deltas** — is **also wrong and mathematically invalid:** B's evaluation windows
overlap and repeated HOLD reaffirmations double-count the same exposure; an
unmanaged holding (NVDA) has **no** B bet yet still drives C's selection effect; and
per-bet deltas are unweighted by position size whereas a portfolio selection effect
is weight-weighted. An unweighted sum is therefore the wrong identity. **Correct
relationship:** both C and B derive from the **same canonical position/return
primitives** in the `validated_snapshot` / `validated_snapshot_period` records,
joined through **one persisted, versioned `exposure_allocation` record** (spec §8):
position-day ownership (which `validated_decision`, if any, governs each position-day
and its effective window), the decision→exposure mapping (shares × window per
decision), and an overlap-precedence rule (so no position-day is double-owned or
orphaned). **Ownership is a CLOSED classification — the mapper may not fail open.**
Every position-day carries exactly one of `decision_owned` |
`deliberately_unmanaged:<policy-id>` | `expected_but_missing:<reason>` (spec §8). Only
the first two may publish or contribute to reconciliation; **`expected_but_missing`
(a holding whose governing decision was lost/failed/omitted by the mapper — e.g. an
AAPL mapping bug) MUST block reconciliation and appear in a value-weighted coverage
denominator.** Without this, a lost decision would be silently reclassified as
"unmanaged," total-C would stay unchanged, B would be compared only to the reduced
managed subset, and the gate would pass despite its own coverage bug. From the
classification, three **separately-named identities** are computed: **(i)
managed / B-attributable selection** (over `decision_owned` position-days), **(ii)
unmanaged selection** (over `deliberately_unmanaged` position-days ONLY, e.g. NVDA —
not a catch-all for decisionless holdings), and **(iii) total C selection = (i) +
(ii)**, publishable only when no `expected_but_missing` weight remains.
**Reconciliation checks identity (i) against B over aligned windows — NEVER total-C
against B, and only once the coverage denominator shows zero `expected_but_missing`
weight.** Comparing total-C to B must fail forever
because NVDA is in C's selection but has no B bet; excluding (ii) from the comparison
is what makes the reconciliation well-posed, while the closed classification keeps
(ii) from absorbing a coverage bug. **Governance is unchanged:** B's per-bet
vs-benchmark delta remains the atomic source of truth the learning loop (spec §5/§8)
reads; C's selection figure (identity iii) is the proof-surface aggregate, never an
independent input to learning. **Divergence of identity (i) from B beyond a stated
tolerance — after the `exposure_allocation` weights, windows, and precedence are
applied — is a fail-loud flag that blocks publishing either number**, never two
coexisting "selection" figures with no adjudication ("which number is right?" must
not return).

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
- **GAP #2 (primary risk):** no clean dated cashflow ledger — and this is a
  **hard proof-blocker, not a mitigated inconvenience.** The earlier draft claimed
  §2.2 "derives security flows from snapshot share-diffs (no ledger needed)" and that
  "the design is feasible *because* §2.2 sidesteps the ledger." **Both claims are
  deleted as false** (see §2.2): a snapshot share-diff is not a dated flow, it cannot
  place a mid-period trade in time, and an internal buy/sell is not even an external
  TWR flow. `rsu_vest_events` covers dated RSU inflows and `fx_rates` covers FX, but
  every other buy/sell/transfer flow needs **dated fill provenance that does not yet
  exist.** Consequence: without dated fills, C produces a **diagnostic only** (§6 0a),
  never a proof/headline return. The share-diff decomposition is a corruption
  *detector* and a rough diagnostic — it is **not** a substitute for a fill ledger.
  This gap, and the unattributed-cash handling, are where the design most needs
  adversarial review.

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

**Proof vs. diagnostic — which inputs each surface may read.** Any **headline /
proof** number ("are we beating the market?") reads **only** validated spine
periods (`validated_snapshot_period` over per-item-bound `validated_snapshot`s, with
dated-provenance flow reconciliation — spec §2A) and must pass the §2.5 B↔C
reconciliation gate. The steps below that read `portfolio_snapshots` directly
(step 1) are the **pre-spine diagnostic** path (§6 0a): their output is labelled
diagnostic-only, may not feed learning, and may not be shown on a proof surface.
Once the spine exists, the same pipeline runs over spine records and its output is
eligible to be a proof number.

1. **Canonical value series:** dedup `portfolio_snapshots` to one period-end per
   date; backfill pre-June via `net_worth_backfill` archived TSVs. Split into
   liquid-investable vs pension/real-estate buckets.
2. **Flow derivation + corruption gate:** per consecutive pair, per position, split
   Δvalue into market-return vs share-flow (§2.2); run the fail-loud reconciliation
   gates (zero-out / partial-drop >X% / stale price / split-spinoff / cross-account
   internal-transfer) and **quarantine any period that fails** — no return for it;
   reconcile surviving flows against **dated provenance** (`rsu_vest_events` +
   fill/transfer IDs; an `executed_live` proposal is a cross-check, not provenance,
   §2.2). A residual cash-balance change that is not classified by a dated
   interest/deposit/withdrawal record is an **UNCLASSIFIED observation that blocks
   the proof number for that period** (§2.3) — it is **not** downgraded to a
   "small residual, confidence-scored" pass.
3. **Sub-period returns:** segment holdings at dated events (§2.2), compute
   position-period returns over actual effective windows, then Modified Dietz on
   **external flows only** (deposits/withdrawals/vests — not internal trade
   notionals), USD and NIS (`fx_rates`); geometric link → cumulative TWR; also solve
   IRR (MWR) on the external-flow set. (Proof path only — needs dated fills; endpoint-
   only pairs are diagnostic, §6 0a.)
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
- **Reconstructed flows are approximate — but corruption and unclassified movements
  are NOT a confidence band.** A genuinely approximate figure (a modest mid-period
  flow whose date is known but imprecise) gets a data-confidence score per period.
  Two things are categorically different and get **no** confidence score:
  - a **corruption event** (a zero-out, an unreconciled partial drop >X%, stale
    prices, an unadjusted split/spinoff, an unmatched cross-account internal
    transfer — §2.2) **fails loud and quarantines the period**; and
  - an **unclassified cash movement** (§2.3) or a **flow with no dated provenance**
    (§2.2) **blocks the proof number** for that period — it is retained as an
    unclassified observation, never smoothed into a "small residual, lower
    confidence" pass.
  The distinction is load-bearing — the old design's mistakes were letting a silent
  deletion ride as "lower confidence," and letting repeated sub-threshold cash
  understatements accumulate under a confidence label. Confidence scores are for real
  approximation; corruption and unclassified/undated movements yield no proof number.
  (Argosy's output-trust doctrine: every number auditable to raw rows.)
- **Benchmark choice is a judgment** (ACWI vs VT vs a NIS-hedged blend) — make it
  explicit and configurable; show which was used.

---

## 6. Phased build (each phase independently shippable)
- **0a — Pre-spine DIAGNOSTIC (renamed; NOT a headline/proof number):** dedup
  series → position-decomposition TWR (USD+NIS) vs ACWI, with confidence band, on
  existing snapshots + new `benchmark_prices`. **Because it reads raw snapshots
  without the spine's per-item integrity binding and without dated flow provenance,
  its output is an explicitly-labelled *diagnostic* — it may NOT be shown on any
  "are we beating the market?" proof surface and may NOT feed learning.** This
  resolves the earlier contradiction with §9 (a headline is diagnostic-only until it
  reads validated spine periods and reconciles with B). It is a build/plumbing
  smoke, not the product number.
- **0a-proof — Headline over the spine:** the same computation run over
  `validated_snapshot_period` records (dated-provenance flows, per-item binding),
  passing the §2.5 B↔C reconciliation gate. **This** is the number a proof surface
  may show. Requires the spine (spec §2A) to exist.
- **0b — Policy benchmark + Brinson attribution** (allocation/selection/cash/FX)
  via the sleeve map. This is the "is our stock-picking costing us?" answer, and it
  reads spine periods (proof surface).
- **0c — Risk-adjusted + history backfill** (net_worth_backfill) + durable
  `portfolio_return_periods` + `/performance` API/UI + IRR.

---

## 7. Open questions for the reviewer
1. **Share-diff flow logic (§2.2):** is decomposing Δvalue into
   price-return vs share-flow sound given snapshots can carry stale/re-imported
   prices and multiple rows per date? Where could it double-count or misattribute
   a corporate action (split/spin — e.g. the NOW 5:1-class events)?
2. **Unattributed cash (§2.3, §3.2):** the design now **blocks** the proof number on
   any unclassified cash movement rather than confidence-flagging it. Is that the
   right line, or is there a class of small, provably-bounded cash noise that should
   be allowed to pass with a caveat? (Note the accumulation risk: many sub-threshold
   passes sum to a material error.)
3. **Base currency:** NIS headline with USD secondary — agreed? Or NIS-hedged
   policy benchmark to strip FX from the *skill* comparison?
4. **Benchmark selection:** ACWI vs VT vs a custom blend matching the plan's
   geographic mix — which is the fair "market"?
5. **Pension/RE separation (§2.3):** correct to exclude from headline TWR, or does
   the family want a single total-wealth return despite the repricing noise?
6. **History depth:** is a 4.5-month provisional number worth shipping now, or
   backfill first (0c before 0a) so the first number shown isn't noise?
