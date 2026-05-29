# Anomaly Detection + RSU Pre-Vest Planning — Design

**Status:** Pending Ariel approval (auto-mode authorization received 2026-05-29). Codex tandem review pending.
**Date:** 2026-05-29
**Author:** Claude (Opus 4.7), with codex tandem review queued.
**Sibling spec:** [`2026-05-29-plan-execute-monitor-reorg-design.md`](2026-05-29-plan-execute-monitor-reorg-design.md). This is sprint #2; both share the unified implementation plan.

## Problem

Ariel's three-flow framing put Flow 2 (household maintenance) at: "Monthly, user uploads expenses + portfolio → agents sort, look for anomalies, and suggest new allocation according to plan (new stocks, buy/sell, **warn about RSU not vested**, etc.)".

The sibling spec covers the upload + allocation-suggestion side. This spec covers the gaps:

- **Anomaly detection on transactions** — partial scaffolding exists (`AnomalyDetectionAgent`, `ExpenseReviewQueue`, `AnomalyCard` UI types) but the actual detection logic is shallow: no per-merchant rolling baselines, no historical state-tracking across runs, no transaction-row UI surface.
- **RSU pre-vest planning** — no forward-looking visibility today. The user's mental model: "$200K vesting in Sep — plan for it" (paired with the unallocated-cash flow when the vest converts to cash). Not tax-window timers, not concentration warnings, not retrospective sell-too-soon educational content — those are out of scope for v1.

## Goal

Ship four anomaly-detector buckets covering ~80% of routine bookkeeping surprises, surfaced inline on transaction rows + the existing `/home` `AnomalyHighlights` component. Add a forward-looking pre-vest planning surface tied to the sibling spec's Holistic Timeline + life-events flow.

## Non-goals

- No RSU tax-window timer / LTCG countdown (user rejected; out of scope).
- No concentration cap warnings (out of scope; existing `ConcentrationAnalystAgent` keeps doing what it does).
- No sell-too-soon retrospective ("you sold X days before LTCG eligibility").
- No new `/expenses` Anomalies sub-tab (user rejected — anomalies are inline + existing Home tile, not a workbench surface).
- No auto-promotion of accepted allocation_actions to /proposals (user confirmed manual broker step; sibling spec design holds).
- No Red-Flag Strip integration for anomalies (user picked existing AnomalyHighlights; spec #1's Red-Flag Strip remains exclusive to monitor flags).

## Section 1 — Anomaly detection: four buckets

All four buckets ship in v1. Each bucket bundles related detection patterns sharing infrastructure.

### Section 1.1 — Bucket A: Amount outliers

Two patterns, share a new per-merchant + per-category rolling stats table.

**Pattern A1 — Category robust outlier** (codex IMPORTANT — use median+MAD per existing `expense_dashboard.py:456-467` precedent):
- For each new transaction in category C: compute the median + MAD (Median Absolute Deviation) of OTHER transactions in C over trailing 180 days.
- Robust z-score: `r = (amount - median_C) / (1.4826 * MAD_C)` (the 1.4826 scales MAD to match stdev for normal distributions).
- Fire when `r ≥ 3` AND `abs(amount) ≥ ₪200` (avoid noise on micro-transactions).
- Materiality scaled by robust z: 3-4 = info, 4-6 = warning, ≥6 = critical.
- Why robust stats: real spend distributions are heavy-tailed (annual insurance, year-end gifts). Raw stdev gets corrupted by exactly the outliers we're trying to detect.

**Pattern A2 — Merchant spike:**
- For each new transaction at merchant M: compare `amount_nis` to the trailing 6-month mean of all transactions at M.
- Fire when `amount ≥ 3 × mean_M` AND `mean_M ≥ ₪50` (avoid noise on rare merchants) AND M has ≥3 prior occurrences (need baseline).
- Materiality: warning by default; critical if `amount ≥ 5 × mean_M`.

**New table `merchant_rolling_stats`:**
```sql
CREATE TABLE merchant_rolling_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  merchant_normalized TEXT NOT NULL,
  category_id INTEGER NULL REFERENCES expense_categories(id),
  window_start DATE NOT NULL,
  window_end DATE NOT NULL,
  txn_count INTEGER NOT NULL,
  median_nis NUMERIC(12,2) NOT NULL,
  mad_nis NUMERIC(12,2) NULL,  -- NULL when txn_count < 2; Median Absolute Deviation
  mean_nis NUMERIC(12,2) NOT NULL,  -- kept for dashboard backward-compat
  stdev_nis NUMERIC(12,2) NULL,  -- kept for dashboard backward-compat
  min_nis NUMERIC(12,2) NOT NULL,
  max_nis NUMERIC(12,2) NOT NULL,
  first_seen_at DATE NOT NULL,
  last_seen_at DATE NOT NULL,
  computed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (user_id, merchant_normalized, category_id, window_end)
);
CREATE INDEX ix_merchant_rolling_stats_user_merchant 
  ON merchant_rolling_stats (user_id, merchant_normalized);
```

Computed by a nightly cron job (`argosy/services/anomaly/rolling_stats.py::recompute_merchant_stats(user_id)`) that runs after the daily-automation pipeline from spec #1. Window = trailing 180 days from the latest transaction.

### Section 1.2 — Bucket B: Recurring-pattern anomalies

Two patterns, share a new recurring-pattern learner.

**Pattern B1 — Fee-waiver / promotion missing** (extends existing Card 2923 watchlist):
- Existing `watchlist_seed.yaml` has `discount_bank_card_2923_fee_waiver` with expected charge+discount pattern.
- Gap: `AnomalyDetectionAgent` evaluates per-statement but doesn't track historical state.
- New: `argosy/services/anomaly/state_tracker.py` records each watchlist entry's status per **statement** (not per calendar month) in a new `watchlist_observations` table.

**State machine** (codex BLOCKER #1 integration — disambiguate statement-missing vs pattern-missing):

| Statement state | Pattern state | observation status |
|---|---|---|
| Statement present | Charge + discount both found | `MATCHED` |
| Statement present | Charge found, discount missing | `MISSING` |
| Statement present | Charge missing entirely (might be a billing-period gap) | `PARTIAL` |
| Statement absent for the period | — | `UNKNOWN` |

**Fire rule**: fire `fee_waiver_missing` flag ONLY on transition `MATCHED → MISSING` between consecutive observations (where `observation_period_n` and `observation_period_n-1` are both on statements that exist). Do NOT fire on:
- `UNKNOWN → MISSING` — we don't know what last month looked like; could be first-time observation
- `MATCHED → UNKNOWN` — statement missing this period; surface a separate `statement_late` flag if needed but not `fee_waiver_missing`
- `PARTIAL → MISSING` — charge wasn't observed last period so the waiver-vs-no-waiver distinction is moot
- `MISSING → MISSING` — already alerted previously; resurfacing is duplicate noise

This explicit table is the contract for `state_tracker.py`. Unit tests assert each cell.

**Pattern B2 — Recurring-charge missing:**
- Learn recurring patterns: for each merchant M with ≥3 transactions in trailing 12 months at roughly the same dollar amount (±15%) at roughly monthly cadence (28-32 day intervals), flag as recurring.
- Persist learned patterns in new `recurring_charge_patterns` table.
- Fire when: an active recurring pattern's expected charge window passes (cadence + 7 day grace) with no match.
- Materiality: warning. Subscription cancellation is typically intentional but unreported; payment failures are not.

**New tables:**
```sql
CREATE TABLE watchlist_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  watchlist_entry_id TEXT NOT NULL,  -- e.g. 'discount_bank_card_2923_fee_waiver'
  observation_period DATE NOT NULL,  -- first day of month
  status TEXT NOT NULL CHECK (status IN ('MATCHED','MISSING','PARTIAL','UNKNOWN')),
  evidence_tx_ids TEXT NOT NULL DEFAULT '[]',  -- JSON list of matching tx ids
  computed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (user_id, watchlist_entry_id, observation_period)
);

CREATE TABLE recurring_charge_patterns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  merchant_normalized TEXT NOT NULL,
  expected_amount_nis NUMERIC(12,2) NOT NULL,
  amount_tolerance NUMERIC(4,3) NOT NULL DEFAULT 0.15,  -- ±15%
  cadence_days INTEGER NOT NULL,  -- learned median
  cadence_tolerance_days INTEGER NOT NULL DEFAULT 7,
  first_seen DATE NOT NULL,
  last_seen DATE NOT NULL,
  occurrence_count INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','dormant','user_dismissed')),
  computed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (user_id, merchant_normalized, expected_amount_nis)
);
```

### Section 1.3 — Bucket C: Merchant-cache anomalies

Two patterns, share existing `MerchantCategoryCache` (no new tables).

**Pattern C1 — Novel merchant (first-seen ever):**
- Fire when a transaction's `merchant_normalized` has no prior occurrence in `expense_transactions` for this user.
- Rate-limited: don't fire on month #1 (would spam with every merchant being novel); kick in only when user has ≥100 historical transactions.
- Materiality: info — informational, not actionable. Surfaces in AnomalyHighlights but no Red-Flag-Strip producer.

**Pattern C2 — Category drift:**
- Fire when `MerchantCategoryCache.last_hit_at < today - 180 days` AND recent transactions at the same merchant use a different category than the cache rule.
- Indicates the cache rule is stale and the merchant's actual usage pattern shifted.
- Materiality: warning. Pairs with "click to confirm new category" inline action.

### Section 1.4 — Bucket D: Cross-card duplicate / fraud

Single pattern, uses transaction table only (no new state).

**Pattern D1 — Cross-card duplicate:**
- Fire when two transactions exist within 7 days with same `merchant_normalized` AND `amount_nis` (±₪0.50 to allow rounding) AND from different `statement_id`s that belong to DIFFERENT cards (i.e. different `account_kind` or different last-4 digits).
- Excludes: same card (legit duplicate purchases), card payment transactions (`is_card_payment=TRUE`).
- Materiality: warning by default; critical if `amount_nis ≥ ₪1000`.

### Section 1.5 — Detection producer architecture

```
                      ┌──────────────────────────────┐
                      │  Nightly cron (post-ingest)  │
                      │  argosy/services/anomaly/    │
                      │  detector.py::run_all()      │
                      └──────────────┬───────────────┘
                                     │
                ┌────────────────────┼────────────────────────┐
                │                    │                        │
                ▼                    ▼                        ▼
        ┌────────────┐     ┌──────────────┐         ┌──────────────────┐
        │  Bucket A  │     │   Bucket B   │         │ Buckets C + D    │
        │  rolling   │     │  state       │         │  cache + tx      │
        │  stats     │     │  tracker     │         │  scan            │
        └─────┬──────┘     └──────┬───────┘         └────────┬─────────┘
              │                   │                          │
              └───────────────────┼──────────────────────────┘
                                  ▼
                        ┌─────────────────────┐
                        │ ExpenseReviewQueue  │
                        │ rows                │
                        │ (existing table)    │
                        └──────────┬──────────┘
                                   │
                          ┌────────┴────────┐
                          ▼                 ▼
                   ┌─────────────┐   ┌──────────────┐
                   │ AnomalyHighl│   │ Inline badge │
                   │ on /home    │   │ on tx row    │
                   └─────────────┘   └──────────────┘
```

**Idempotency:** each detector run is deterministic given the same input snapshot. Re-running over the same data must NOT create duplicate `ExpenseReviewQueue` rows. Implemented via a deterministic `dedup_key` column per anomaly type.

## Section 2 — UI surfaces

### Section 2.1 — Inline badge on transaction row

New visual treatment in `ui/src/components/expenses/transactions-table.tsx`. Per row:

- Right of the merchant column, a small icon appears if `tx.id` has any open `ExpenseReviewQueue` rows.
- Icon per kind:
  - Bucket A (amount): ⚠ rose if critical, ⚠ amber if warning, ℹ slate if info
  - Bucket B (recurring): 🔁 amber for missing-recurring, 🎫 amber for fee-waiver-missing
  - Bucket C: ✨ slate for novel-merchant, 🔀 amber for category-drift
  - Bucket D: 🚨 rose for cross-card-duplicate
- Click → opens existing `TransactionDetailsDialog` with a new "Anomaly" tab listing the open ExpenseReviewQueue rows + per-row Dismiss / Resolve / Open detail action.

### Section 2.2 — `<AnomalyHighlights>` on /home

Existing component at `ui/src/components/expenses/anomaly-highlights.tsx`. Already supports the `AnomalyCard` kind enum: `uncategorized | novel_merchant | large_outlier | fee_waiver_missed | conservation_gap | merchant_spike | new_high_value_merchant`.

Wiring work in this sprint:
- Backend route `GET /api/anomaly/highlights?user_id=` returns the top N (default 5) most-material open ExpenseReviewQueue rows, formatted as `AnomalyCard[]`.
- Map new detector outputs to existing AnomalyCard kinds:
  - `bucket_a_category_z_score` → `large_outlier`
  - `bucket_a_merchant_spike` → `merchant_spike`
  - `bucket_b_fee_waiver_missing` → `fee_waiver_missed`
  - `bucket_b_recurring_missing` → new kind `recurring_missing` (add to UI enum)
  - `bucket_c_novel_merchant` → `novel_merchant`
  - `bucket_c_category_drift` → new kind `category_drift` (add to UI enum)
  - `bucket_d_cross_card_duplicate` → new kind `cross_card_duplicate` (add to UI enum)

Three new UI kinds need icons + colors in `AnomalyHighlights` component.

### Section 2.3 — No new /expenses Anomalies sub-tab

Per Ariel's Q3 answer. Working-the-queue UX is the existing TransactionDetailsDialog (one transaction at a time, contextual). If a workbench-style queue is needed later, it's a follow-on, not v1.

### Section 2.4 — No Red-Flag Strip producer

Anomalies stay in AnomalyHighlights (lower-friction, less interruptive). Red-Flag Strip stays exclusive to monitor flags from sibling spec.

## Section 3 — RSU pre-vest planning

### Section 3.1 — Data source

Spec #1 §3.2 (revised) lands the new `rsu_unvested_grants` table via Schwab parser extension. This spec consumes it.

### Section 3.2 — Pre-vest planning surface

**New service** `argosy/services/rsu_prevest_planner.py`:

```python
def compute_upcoming_vest_outlook(
    user_id: str,
    *,
    horizon_days: int = 90,
) -> UpcomingVestOutlook:
    """Look at rsu_unvested_grants where expected_vest_date is within horizon_days.
    
    For each pending vest, compute three tax-rate scenarios (codex IMPORTANT #4):
      - expected_gross_usd = shares_pending * latest_nvda_price
      - rate_nominal  = plan_draft.assumed_marginal_rate  (the "base" scenario)
      - rate_effective = observed_effective_rate_from_tax_analyst  (low scenario;
        uses actual prior-year effective rate from filed returns)
      - rate_conservative = max(0.47, rate_nominal + 0.05)  (high scenario;
        treats vest income as supplemental withholding-rate-capped)
      - post_tax_{nominal,effective,conservative}_usd = gross * (1 - rate)
      - allocation_suggestion = preview from _allocate_long_term() using the
        NOMINAL post-tax amount (the base case for planning)
    
    Returns UpcomingVestOutlook with all three scenarios surfaced + the base-case
    allocation preview.
    """
```

Three-scenario tax estimates avoid the "one opaque rate hides surprise" failure mode codex flagged. The UI displays all three.

**No firing logic** — this is purely advisory, surfaced as a UI card. No `monitor_flags` rows, no `ExpenseReviewQueue` rows. The user wants visibility, not alerts.

### Section 3.3 — UI surface: `<UpcomingVestCard>`

Mounts on `/retirement` (alongside HolisticTimelineCard from spec #1) AND on the existing `/expenses` RSU sub-tab (since RSU is in the expenses domain too).

Card content per upcoming vest:
- **Header**: "Vesting in N days: 1,000 NVDA shares (grant ABC)" + expected vest date
- **Body**: estimated gross USD + three-scenario post-tax estimates (nominal / effective / conservative — see §3.2) + the proposed allocation preview based on the NOMINAL post-tax amount ("If this lands as cash today, suggest 60% Growth split → QQQM + SCHG")
- **Tax assumption footnote**: explicit "Nominal rate = plan-assumed %; effective rate = your prior-year filed %; conservative = max(47%, nominal+5%)." So the user sees WHICH rate drives the headline number.
- **Footer**: "Add as life event →" CTA (creates a `life_events` row of kind=`asset_event:other_asset_acquired` with the nominal-post-tax amount, pre-populating the form from spec #1 §4)

### Section 3.4 — Integration with Holistic Timeline

The HolisticTimelineCard from spec #1 §3 already reads `rsu_unvested_grants` to render future-vest markers on the timeline. This spec's `UpcomingVestCard` is the DETAILED breakdown for the nearest few vests. Together they answer:
- Timeline → "what's the long-range vesting schedule?"
- UpcomingVestCard → "what's coming in the next 90 days and what should I do with it?"

## Section 4 — Schema changes

Three migrations.

**Migration 0044 — `merchant_rolling_stats` table** (Section 1.1).

**Migration 0045 — `watchlist_observations` + `recurring_charge_patterns` tables** (Section 1.2).

**Migration 0046 — `ExpenseReviewQueue` extensions** — add columns to existing table:
```sql
ALTER TABLE expense_review_queue ADD COLUMN materiality TEXT NULL 
  CHECK (materiality IN ('info','warning','critical'));
ALTER TABLE expense_review_queue ADD COLUMN dedup_key TEXT NULL;
ALTER TABLE expense_review_queue ADD COLUMN bucket TEXT NULL
  CHECK (bucket IN ('amount','recurring','cache','duplicate'));
CREATE UNIQUE INDEX ix_expense_review_queue_dedup 
  ON expense_review_queue (user_id, dedup_key) 
  WHERE dedup_key IS NOT NULL AND status = 'open';
```

**Dedup-key formulas** (codex IMPORTANT #3 — each per-bucket dedup_key is stable across reruns + survives rule-param tweaks via the version prefix):

```
v1|<bucket>|<user_id>|<stable-entity>|<period-or-window>|<rule-params-hash>
```

Per-pattern formulas:

| Pattern | Formula |
|---|---|
| A1 category robust outlier | `v1\|a1\|u:<user_id>\|cat:<category_id>\|tx:<tx_id>\|win_end:<yyyy-mm-dd>\|thr:3\|min:200` |
| A2 merchant spike | `v1\|a2\|u:<user_id>\|m:<merchant_norm>\|tx:<tx_id>\|win_end:<yyyy-mm-dd>\|mult:3` |
| B1 fee-waiver missing | `v1\|b1\|u:<user_id>\|watch:<entry_id>\|period:<yyyy-mm-01>\|transition:matched_missing` |
| B2 recurring missing | `v1\|b2\|u:<user_id>\|pat:<pattern_id>\|expected:<yyyy-mm-dd>` |
| C1 novel merchant | `v1\|c1\|u:<user_id>\|m:<merchant_norm>\|first_tx:<tx_id>` |
| C2 category drift | `v1\|c2\|u:<user_id>\|m:<merchant_norm>\|cache_cat:<id>\|obs_month:<yyyy-mm>` |
| D1 cross-card duplicate | `v1\|d1\|u:<user_id>\|pair:<min_tx_id>-<max_tx_id>` |

Version prefix `v1` allows future rule changes (e.g. switching threshold from 3 to 2.5) without false suppression — new key, new flag.

(no new table for unvested grants — that lands in spec #1 sprint commit #6.)

## Section 5 — Sprint commit order

12 commits (codex IMPORTANT #7 — split coarse migrations into per-table commits). Per [[feedback_work_style_long_sprints]] — codex zigzag on the money-math + parser-touching ones, SDD update per commit.

| # | Commit | Codex zigzag | Notes |
|---|---|---|---|
| 1 | Migration 0044 — `merchant_rolling_stats` table + model | **Yes** | Per-migration commit (codex #7) |
| 2 | Migration 0045 — `watchlist_observations` + `recurring_charge_patterns` tables + models | **Yes** | Per-migration commit |
| 3 | Migration 0046 — `ExpenseReviewQueue` extensions (materiality, dedup_key, bucket) | **Yes** | Per-migration commit; dedup-key index critical |
| 4 | `merchant_rolling_stats` nightly recompute service — uses median+MAD per codex finding | **Yes** | Robust statistical math |
| 5 | Bucket A detectors (A1 category robust-outlier + A2 merchant spike) writing to ExpenseReviewQueue with dedup_keys | **Yes** | Money-math; uses rolling stats + dedup formulas |
| 6 | Bucket B state-tracker service (watchlist_observations + 4-state transition rules from §1.2) | **Yes** | State-machine contract; Card 2923 critical path |
| 7 | Bucket B recurring-pattern learner + missing-recurring detector | **Yes** | Pattern-inference risk |
| 8 | Bucket C novel-merchant + category-drift detectors | No | Uses existing MerchantCategoryCache primitives |
| 9 | Bucket D cross-card duplicate detector + tests | No | Single-pattern, transaction-table only |
| 10 | `GET /api/anomaly/highlights` route + AnomalyCard mapping + 3 new UI kinds (recurring_missing, category_drift, cross_card_duplicate) with icons/colors | No | API + UI wiring |
| 11 | Inline anomaly badges on transactions-table.tsx + TransactionDetailsDialog new "Anomaly" tab | No | UI only |
| 12 | RSU pre-vest planner service (three-scenario tax estimate per codex #4) + `<UpcomingVestCard>` on /retirement + /expenses/rsu | **Yes** | Money math (tax estimation, allocation preview) |

**Parallelism note** (codex IMPORTANT #6): commits #1-3 (migrations) must follow sibling spec commit #6 (rsu_unvested_grants table from Schwab parser extension). But commits #10-11 (UI wiring) can begin in parallel with sibling spec commits #7+ once the API contract is locked. The unified implementation plan schedules accordingly.

## Section 6 — Risk register

| Risk | Mitigation |
|---|---|
| Bucket A z-score false positives on bursty merchants (e.g. annual insurance) | Pattern A1 requires `txn_count ≥ 6` in baseline; pattern A2 requires `≥3 occurrences`. Both seasonally-rare patterns won't meet the bar; expected behavior. |
| Bucket B fee-waiver false-positive when statement is delayed | Watchlist observation runs per-statement, not per-calendar-month. If the discount-bank statement is late, the observation period for that user just shifts. |
| Bucket B recurring-pattern over-learns (false patterns from coincidence) | Require ≥3 occurrences AND ≥80% cadence consistency before activating a pattern. User can dismiss via UI which sets status='user_dismissed'. |
| Bucket C novel-merchant spams in early adoption | Rate-limit: only fire when user has ≥100 historical transactions. |
| Bucket D false positive on legit duplicate (paying same vendor twice on same day from two cards) | Allow user dismissal at the inline badge (sets ExpenseReviewQueue.status='resolved'). |
| RSU pre-vest planning shows wrong tax estimate | Use the same marginal-rate assumption as the tax_analyst agent. Surface the assumption explicitly: "Estimated using your plan's assumed marginal rate of X%". |
| `rsu_unvested_grants` table dependency on spec #1 commit #6 | Sequenced after spec #1 commit #6 in the unified plan. |

## Section 7 — Dependencies between this spec and sibling spec

This spec depends on:
- **spec #1 commit #6** — `rsu_unvested_grants` table + parser extension (for §3 RSU pre-vest planning)
- **spec #1 commit #2** — schema migrations 0041-0043 land first

This spec contributes back to:
- **spec #1 §4 life-events** — `<UpcomingVestCard>` has CTA to create a `life_events` row pre-populated from the upcoming vest

No circular dependency. Unified plan must order: spec #1 commits #1-6 → spec #2 commit #1 → continue interleaved.

## Section 8 — Codex tandem review summary

**Verdict:** APPROVE_WITH_CONDITIONS (run 2026-05-29).
**Session:** `tools/codex-tandem/sessions/2026-05-29-anomaly-detection-rsu-prevest-design/`.

**BLOCKER (integrated above):**
1. Bucket B month-skip semantics — 4-state machine (MATCHED / MISSING / PARTIAL / UNKNOWN) with explicit transition rules in §1.2.

**IMPORTANTs (all integrated):**
2. Bucket A uses median+MAD robust stats (not raw mean+stdev) — §1.1.
3. Per-bucket dedup_key formulas with `v1` version prefix — §4 migration 0046.
4. Three-scenario tax estimate (nominal/effective/conservative) for pre-vest planning — §3.2/3.3.
5. Schwab Deposit row shape verification deferred to commit #12 with real-fixture validation.
6. Sibling-spec serialization is partial — UI wiring (commits #10-11) can begin once API contract locks, parallel with sibling commits #7+. Plan schedules accordingly — §5.
7. Coarse migration grouping split — one migration per commit (#1, #2, #3 separate) — §5.

**NICEs (acknowledged):**
8. `AnomalyHighlights` UI must branch by `kind`, not just severity. Already in §2.2 mapping table.
9. Inline badges confirmed net-new scope. Already in §2.1.
