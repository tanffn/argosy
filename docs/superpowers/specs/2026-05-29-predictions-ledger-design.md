# Predictions Ledger + Outcome Evaluator + Source-Reliability — Design

**Status:** Pending Ariel approval. Codex single-dispatch review COMPLETE (2026-05-29) — verdict BLOCK with 3 BLOCKERs + 4 IMPORTANTs; all integrated below. See [Section 11](#section-11--codex-tandem-review-summary) and the inline "Codex BLOCKER N" / "Codex IMPORTANT N" annotations throughout.
**Date:** 2026-05-29
**Author:** Claude (Opus 4.7), with codex tandem review integrated 2026-05-29.
**Sibling specs:**
- [`2026-05-29-plan-execute-monitor-reorg-design.md`](2026-05-29-plan-execute-monitor-reorg-design.md) — ships `news_signals`, `plan_monitor`, daily-automation pipeline, Discord listener (the producers this spec consumes).
- [`2026-05-29-anomaly-detection-rsu-prevest-design.md`](2026-05-29-anomaly-detection-rsu-prevest-design.md) — sibling sprint #2.
- Spec A (Jobs registry + admin UI) — registers this spec's evaluator + backfill jobs.
- Spec B (general state-observer agent) — one of the internal prediction producers.

## Problem

The user's first instinct was a Discord-only 2-week backtest: "run the alpha-report channel against historical price data, see if the calls actually worked." That's the right *measurement* but the wrong *scope*. Within hours the framing widened:

> "Maybe we should do that in general — for all data sources — this way we can adaptively change the weights. What works better for us → will give us maximum gains."

A per-source backtest baked into the Discord listener is architecturally wrong for three independent reasons:

1. **Duplication.** Every source (Discord, RSS, 13F filings, TipRanks, CapitolTrades, SEC Form 4, internal `per_position_thesis`, internal `news_signal_analyst`, internal Spec-B state-observer, internal `plan_monitor` flags, manual user gut-calls) would need its own backtest harness with its own outcome rules. Six harnesses with subtly different definitions of "did the call work?" guarantee that the numbers won't be comparable.
2. **No meta-learning surface.** A per-source script in `discord_listener.py` can answer "Discord was 47% right last 14 days." It cannot answer "Discord at 47% is twice as accurate as the SEC 13F follow-trade signal at 23% — weight Discord 2× when both fire on the same ticker." The whole point of measuring is to feed the result back to consumers; that requires a shared schema.
3. **Internal agents go un-measured.** Argosy's *own* agents make predictions every day. `per_position_thesis` emits HOLD/BUY/TRIM/SELL verdicts. `news_signal_analyst` emits materiality classifications. Spec B's state-observer fires "something is shifting" flags. `plan_monitor` fires drift / MC-regression flags. **These are predictions too.** Per Ariel's binding decision: the ledger measures the reliability of Argosy's own agents alongside external sources, and the system is willing to learn it is wrong about itself.

The right architecture: one `predictions` table that any source can write to, one outcome evaluator that scores them all with a deterministic rule, one `source_reliability` view that consumers read to weight future signals. Discord is the first worked example because the user has a 14-day backlog ready to backfill; the design is general from commit #1.

## Goal

Ship a unified predictions ledger + deterministic outcome evaluator + source-reliability view + consumer-integration wiring. Internal agents are first-class sources. Discord 14-day backfill is the worked example proving the schema handles free-text alpha calls; the same schema must accommodate structured 13F holdings and qualitative state-observer flags without modification.

## Non-goals

- **No new prediction *producers* in this sprint.** Discord, news_signal_analyst, per_position_thesis, state_observer, plan_monitor already produce signals — we wire them into the ledger via thin writer adapters. New sources (TipRanks, CapitolTrades, SEC 13F) get adapter stubs but aren't activated until the user opts in.
- **No automated trading on reliability scores.** Reliability flows into recommendation *weighting*, never into auto-execution. The user remains the decision-maker (per [[feedback_ask_dont_assume]]).
- **No retraining of the agents themselves.** If `news_signal_analyst` is found to be 30% accurate, we downweight its output downstream; we do NOT fine-tune the model. Prompt iteration on under-performing agents is a separate manual workflow.
- **No Bayesian / posterior framework in v1.** Reliability is plain hit-rate + average P/L per call. A future spec can layer Beta priors / hierarchical models on top of the same ledger.
- **No public leaderboard / cross-user reliability sharing.** Single-user today; multi-tenant later.
- **No `unparseable` retry loop.** If a source emits something the writer adapter can't structure as a prediction, the prediction is logged with `outcome_kind='unparseable'` and excluded from reliability stats. Re-parsing is a future spec.

## Sprint commit table

7 commits, ~1 with optional UI commit #8.

| # | Commit | Codex zigzag | Notes |
|---|---|---|---|
| 1 | Migration 0048 — `predictions` table + model + indexes | **Yes** | Per-migration commit. Multi-source schema; codex probes shape variance + index plan. |
| 2 | Migration 0049 — `prediction_outcomes` table + model + indexes | **Yes** | Per-migration commit. Outcome enum + scoring-method enum + replay-cursor design. |
| 3 | Writer adapters — `argosy/services/predictions/writers.py` with `discord`, `news_signal_analyst`, `per_position_thesis`, `state_observer`, `monitor_flag` writers + call-site wiring | **Yes** | Touches 5 producers; per-source idempotency contract. |
| 4 | Outcome evaluator service + Spec-A job registration — `argosy/services/predictions/evaluator.py` registered as `predictions-evaluate-due` daily job | **Yes** | Money path: price-data adapters + scoring rule. Determinism + edge cases. |
| 5 | `source_reliability` view + service — SQL view + `argosy/services/predictions/reliability.py` accessor with caching | **Yes** | Aggregation correctness + rolling-window semantics. |
| 6 | Consumer integration — `synthesizer` / `news_signal_analyst` / `per_position_thesis` / Spec-B `state_observer` read reliability + apply weights | **Yes** | Multi-consumer money path. Anti-feedback-loop contract. |
| 7 | Discord 14-day backfill job — `predictions-backfill-discord` registered with Spec A + `POST /api/jobs/predictions-backfill-discord/run-now` route | **Yes** | Real-data dependency; assumes Discord daemon running. |
| 8 (opt) | `/admin/source-reliability` UI page — leaderboard with last-N-day rolling reliability + per-source drill-down | No | UI only; surfaces what's already in the view. |

Per [[feedback_work_style_long_sprints]] — long sprint, codex zigzag per risky commit, SDD update per commit, blockers logged to codex not user.

## Section 1 — The predictions schema

### Section 1.1 — Why one table, not one-table-per-source

A 13F filing is structured (`fund_id, ticker, shares, action, filing_date`). A Discord call is free-text (`@trader: "long NVDA $145 → $180 stop $135 by next week"`). A state-observer flag is qualitative (`{"flag": "concentration_growing", "ticker": "NVDA", "direction": "concerning"}`). These look incompatible.

But every prediction collapses to the same answerable question at outcome time: **"Within timeframe T, did the price action of ticker X confirm or deny the direction the source asserted?"** The four pieces — ticker, direction, timeframe, optional levels — are the universal core. Source-specific shape lives in `raw_text_ref` + the source-specific writer's job to extract those four fields. Unextractable signals are still logged (so we have coverage data) but get `outcome_kind='unparseable'` at evaluation time.

This is the codex-probe-worthy core decision: **one ledger, source adapters do the extraction work, outcome evaluator is source-agnostic**.

### Section 1.2 — `predictions` table

Full DDL in [Appendix A](#appendix-a--predictions-ddl). Conceptual columns:

| Column | Why |
|---|---|
| `id` | PK. |
| `source` | Enum: `discord`, `news`, `sec_form_4`, `tipranks`, `sec_13f`, `capitoltrades`, `internal_per_position_thesis`, `internal_news_signal_analyst`, `internal_state_observer`, `internal_monitor_flags`, `manual_user`. **11 values** in v1; enum extensible via migration. CHECK constraint enforces. |
| `source_ref` | TEXT, JSON-encoded source identifier. For Discord: `{"channel_id": ..., "message_id": ...}`. For news_signal_analyst: `{"news_signal_id": 423}`. For per_position_thesis: `{"draft_id": 12, "ticker": "NVDA"}`. For 13F: `{"filing_id": "0001234-25-000001", "fund_id": "BRK"}`. Free-form per source; the writer adapter for that source defines the shape. |
| `ticker` | NULLABLE. NULL for multi-ticker or macro predictions (e.g. "I think the Fed cuts → rates down" has no ticker). When NULL, outcome rule reads `multi_ticker_json` for the basket. |
| `direction` | Enum: `long`, `short`, `neutral`, `multi`. `multi` = basket (e.g. "rotate from tech into utilities" — see multi_ticker_json). |
| `entry_price` | NULLABLE. The price assumed at prediction time (price-data adapter snapshot at `created_at`, NOT a source-asserted "entry"; see hindsight-bias section). |
| `target_price` | NULLABLE. Source-asserted target. Only set when source provides one (Discord call with explicit target, analyst price target). |
| `stop_price` | NULLABLE. Source-asserted stop. Only set when source provides one. |
| `timeframe_days` | NULLABLE INTEGER. How long the source asserts the prediction is valid. NULL → falls back to `default_timeframe_for_source` (Discord = 7d, news_signal_analyst = 7d, per_position_thesis = 30d, state_observer = 30d, monitor_flag drift = 30d, 13F = 90d, manual_user = caller-specified). |
| `multi_ticker_json` | NULLABLE TEXT. JSON array of `{"ticker": "X", "direction": "long", "weight": 0.4}` for multi-ticker baskets. Outcome scored as weighted-average return per Section 5.4. |
| `message_id` | TEXT, denormalized copy of the source-system message-id when applicable (Discord msg id, news url, filing accession id). Indexed for dedup. |
| `event_at` | **DATETIME, NOT NULL.** The real-world prediction time (Discord message timestamp, filing date, news publish time, internal-agent emit time). **This — not `created_at` — is the anchor for entry_price snapshot AND the start of the timeframe window.** Writers MUST pass this explicitly (the backfill case writes 14-day-old `event_at` while `created_at = NOW()`). *Codex IMPORTANT 2 fix.* |
| `created_at` | DATETIME, **DB row insertion** timestamp (DEFAULT CURRENT_TIMESTAMP). Used only for telemetry/audit (e.g. "when did we discover this prediction?"). Diverges from `event_at` on backfill. **NEVER used by scoring math.** |
| `evaluation_due_at` | **DATETIME, NOT NULL.** Computed at write time as `event_at + chosen_window` where chosen_window is determined by the method selection logic of §3.1 step 2 (not raw `timeframe_days`). The evaluator's due-query keys off this column, ensuring §5.5's 30-day-for-long-timeframe-sources policy actually triggers at 30 days instead of waiting the full `timeframe_days`. *Codex BLOCKER 2 fix.* |
| `evaluation_method` | **TEXT, NOT NULL.** The pre-selected evaluation method for this row, computed at write time and stored explicitly. Becomes the FK into the `evaluation_method_registry` table (see §3.4) — NOT a CHECK-enum, so new method versions land via INSERT into the registry, not a migration. *Codex BLOCKER 1 fix.* |
| `raw_text_ref` | NULLABLE TEXT. Pointer to the raw source content — for Discord, `news_signals.id`; for news_analyst, `news_signals.id`; for 13F, the filing URL. Never injected into LLM prompts (codex BLOCKER #2 from Spec #1 carries over). |
| `unparseable_reason` | NULLABLE TEXT. When the writer adapter sees an input it can't structure (e.g. Discord message with no ticker, no direction), it still inserts a row with `direction='neutral'`, `ticker=NULL`, `unparseable_reason='no_ticker_extracted'`. The outcome evaluator marks these `unparseable` at evaluation time and excludes them from reliability stats but counts them in coverage. |

**Indexes** (full DDL in Appendix A):
- `(source, created_at DESC)` — per-source reliability rollups.
- `(ticker, created_at DESC)` WHERE ticker IS NOT NULL — per-ticker historical lookup.
- `(source, message_id)` UNIQUE WHERE message_id IS NOT NULL — dedup on re-ingest.
- `(created_at)` — used by evaluator's "what's due for scoring?" query.

### Section 1.3 — `prediction_outcomes` table

One row per evaluated prediction. Predictions evaluated once and then immutable; if the user later wants a different scoring method, a NEW outcome row is inserted (see Section 5.6 — replay).

Full DDL in [Appendix A](#appendix-a--prediction_outcomes-ddl). Key columns:

| Column | Why |
|---|---|
| `prediction_id` | FK → `predictions.id`. NOT unique — see `evaluation_method` for the second-axis discriminator that allows replay. |
| `outcome_kind` | Enum, 6 values: `hit_target`, `hit_stop`, `expired_neutral`, `expired_positive`, `expired_negative`, `unparseable`. Definitions in Section 5. |
| `pnl_pct` | NULLABLE NUMERIC(7,4). Realized P/L percentage. NULL for `unparseable` and when price data was missing entirely. Signed: positive means the prediction's direction was right. |
| `evaluated_at` | DATETIME. When the evaluator ran. |
| `evaluation_method` | Enum: `target_stop` (when both target + stop set), `fixed_lookahead_7d`, `fixed_lookahead_30d`, `multi_basket_weighted`, `unparseable`. **The (prediction_id, evaluation_method) pair is the natural key** (UNIQUE index). |
| `entry_price_used` | NUMERIC(12,4). The price the evaluator anchored at. Stored explicitly even though it equals `predictions.entry_price` because the evaluator may re-fetch the adapter snapshot if entry_price was NULL (older predictions, manual sources). |
| `exit_price_used` | NUMERIC(12,4). The price at the outcome trigger (target/stop hit, or end-of-window close). |
| `exit_trigger_date` | DATE. The trading day the outcome triggered. For `hit_target` / `hit_stop`, the first day the level was breached; for `expired_*`, the last day of the timeframe window. |
| `evidence_json` | TEXT. JSON containing intraday or daily-bar snapshot used: `{"bars": [{"date": "2026-05-29", "high": 152.10, "low": 148.20, "close": 151.50}, ...]}`. Stored for replayability + user audit. **Bounded to first/last bar + the trigger bar** for predictions longer than 14 days to keep row size sane. |
| `notes` | NULLABLE TEXT. Free-form: "ticker delisted on 2026-04-15", "price data missing for 3 of 7 days", etc. |

**Indexes**:
- `(prediction_id, evaluation_method)` UNIQUE — natural key; supports replay.
- `(evaluated_at)` — for backfill cursors.
- `(outcome_kind)` — for the source_reliability view's hit-rate aggregation.

### Section 1.4 — Schema flexibility across source shapes — worked examples

The schema is identical for all three; what varies is the writer adapter's mapping.

**Discord free-text call:**
```
@trader 2026-05-15 10:23:
  "long NVDA $145 → $180 stop $135 by Friday"
```
Writer extracts: ticker=NVDA, direction=long, entry_price=145, target_price=180, stop_price=135, timeframe_days=5 (until Friday). message_id=`{channel_id}.{message_id}`. raw_text_ref=`news_signals.id`. evaluation_method at outcome = `target_stop`.

**SEC 13F filing (structured):**
```
BRK files 13F-HR 2026-05-15: bought 50M AAPL shares (+12% position size)
```
Writer extracts: ticker=AAPL, direction=long (interpret "+12% position" as bullish), entry_price=NULL (filing has no price assertion — evaluator fills from adapter snapshot at filing_date), target_price=NULL, stop_price=NULL, timeframe_days=90 (13F default). evaluation_method at outcome = `fixed_lookahead_30d` (we don't wait the full 90d for a first read; 30d is a reasonable trade-off — see Section 5.5).

**Internal state_observer qualitative flag:**
```
state_observer fires: {"flag": "concentration_growing", "ticker": "NVDA",
                       "direction": "concerning", "since": "2026-04-01"}
```
Writer extracts: ticker=NVDA, direction=short (interpret "concerning concentration" as bearish-ish — direction here is a coarse confirmation question: "does the price action subsequently confirm the concern?"). entry_price=NULL, target_price=NULL, stop_price=NULL, timeframe_days=30 (state-observer default). evaluation_method at outcome = `fixed_lookahead_30d`.

**Multi-ticker basket (per_position_thesis "rotate"):**
```
per_position_thesis: TRIM NVDA, BUY SCHG, BUY QQQM
```
Writer creates ONE prediction row with ticker=NULL, direction=`multi`, multi_ticker_json=`[{"ticker":"NVDA","direction":"short","weight":0.4}, {"ticker":"SCHG","direction":"long","weight":0.3}, {"ticker":"QQQM","direction":"long","weight":0.3}]`. evaluation_method at outcome = `multi_basket_weighted` (Section 5.4).

The same `predictions` table accommodates all four. No source-specific column. **This is the codex-probe-worthy schema flexibility claim.**

## Section 2 — Writer adapters

Files: `argosy/services/predictions/writers.py`, one writer function per source.

Adapter contract:

```python
def write_prediction(session: Session, *, source: PredictionSource,
                      source_ref: dict, raw: Any) -> int | None:
    """Write a prediction row. Return the new prediction id, or None if
    the input was already recorded (dedup via (source, message_id))."""
```

Per [[feedback_ask_dont_assume]] — writers do NOT guess if a source's content is parseable. They either:
1. Extract a structured prediction → insert with full fields.
2. Determine the input has no actionable shape → insert with `unparseable_reason` set + `direction='neutral'`. Coverage counts this; reliability stats exclude it.
3. The input is a duplicate of an already-recorded prediction → return None, no insert.

### Section 2.1 — Per-source writer call sites

| Source enum | Writer file | Call site | Trigger |
|---|---|---|---|
| `discord` | `writers.py::write_discord_prediction` | `argosy/services/discord_listener.py` after `news_extractor.extract` returns | Real-time on each Discord message |
| `news` | `writers.py::write_news_prediction` | `argosy/services/news_extractor.py` after Stage-1 normalization | Real-time on each RSS / macro feed item |
| `internal_news_signal_analyst` | `writers.py::write_news_signal_analyst_prediction` | `argosy/services/news_analyst_runner.py` after each AnalyzedSignalOut row written | After Stage-2 LLM run completes |
| `internal_per_position_thesis` | `writers.py::write_thesis_prediction` | `argosy/services/per_position_thesis.py::build_position_theses` after the dict is materialized | After plan synthesis writes per-position theses |
| `internal_state_observer` | `writers.py::write_state_observer_prediction` | Spec B's `state_observer.py` after a flag is emitted | After each observer flag |
| `internal_monitor_flags` | `writers.py::write_monitor_flag_prediction` | `argosy/services/plan_monitor.py::check_allocation_drift` and `check_mc_regression` after a MonitorFlag insert | After each plan_monitor flag |
| `manual_user` | `writers.py::write_manual_prediction` | Future `/api/predictions/manual` route | User self-recorded gut-call (out of v1 scope but writer stub lands) |
| `sec_form_4` | `writers.py::write_sec_form4_prediction` | Future `argosy/services/sec_form4_ingest.py` after each filing | Adapter stub only — no producer in v1 |
| `tipranks` | (stub) | Adapter stub only |
| `sec_13f` | (stub) | Adapter stub only |
| `capitoltrades` | (stub) | Adapter stub only |

### Section 2.2 — Idempotency

Critical: every writer must be idempotent. Re-running the news pipeline against the same news_signals must not double-count predictions.

Mechanism: writers compute a `dedup_key` per the same `v1|...` formula family as Spec #2's anomaly buckets:

```
v1|predictions|<source>|<source-stable-entity-id>
```

Per-source:

| Source | Dedup key |
|---|---|
| `discord` | `v1|predictions|discord|<channel_id>.<message_id>` |
| `news` | `v1|predictions|news|<news_signals.id>` |
| `internal_news_signal_analyst` | `v1|predictions|nsa|<news_signals.id>` (one analyst output per news_signal) |
| `internal_per_position_thesis` | `v1|predictions|thesis|<draft_id>.<ticker>` |
| `internal_state_observer` | `v1|predictions|so|<flag_uuid>` |
| `internal_monitor_flags` | `v1|predictions|mf|<monitor_flags.id>` |
| `manual_user` | `v1|predictions|manual|<user-supplied-uuid>` |

Stored on `predictions.message_id` (we reuse the column for this purpose — its semantics are "stable per-source key" not literally a Discord-message id). The UNIQUE index `(source, message_id)` enforces dedup at the DB level. `INSERT ... ON CONFLICT (source, message_id) DO NOTHING` is the writer pattern.

### Section 2.3 — Entry-price snapshot — hindsight-bias killer

**Critical (codex-probe-worthy):** every writer takes a price-data adapter snapshot at the moment of the prediction event. The snapshot is what fills `predictions.entry_price` and what the outcome evaluator anchors at — NEVER a price chosen post-hoc.

**Mechanism (Codex IMPORTANT 2 fix — `event_at` distinct from `created_at`):** writers MUST receive an explicit `event_at: datetime` argument (the real-world prediction time — Discord message timestamp, filing date, etc.). The price snapshot uses `event_at`, NOT `created_at`. The DDL enforces this: `event_at` is NOT NULL and writers must pass it; the historical default-to-CURRENT_TIMESTAMP semantics that previously lived on `created_at` are RESERVED for `created_at` (audit-only), so backfill cannot accidentally drift the snapshot to backfill-run time.

Writers call: `snapshot.get_close_at_or_before(ticker, event_at)`. Returns the most recent daily-close price at or before `event_at`. If the prediction event is mid-trading-day, we use the previous close (deterministic + reproducible; avoids intraday-data flakiness in the adapter).

For event times that are weekends/holidays, `get_close_at_or_before` returns the prior trading day's close. The exit-side timing in Section 5 mirrors this convention. The evaluation window is computed as `[event_at + 1 trading day, event_at + chosen_window_days]` (NOT `created_at + …`).

For tickers the adapter can't price (delisted at time of prediction, unsupported region), the writer logs entry_price=NULL + `unparseable_reason='entry_price_unavailable'`. Outcome at evaluation = `unparseable`.

### Section 2.4 — Internal-agent writer wiring (binding user preference)

Per Ariel's binding decision: **internal agents are first-class sources.** The system measures the reliability of its OWN agents alongside external sources.

Wiring locations:
- `argosy/services/per_position_thesis.py::build_position_theses` — at function exit, for **every** `PositionThesis` (including HOLD), call `write_thesis_prediction(session, source_ref={"draft_id":..., "ticker":...}, thesis=card)`. **Codex BLOCKER 3 fix:** HOLD verdicts ARE logged as predictions with `direction='neutral'` and `evaluation_method='fixed_lookahead_30d'`. This closes the selection-bias hole — an agent that hides behind HOLD now gets its HOLDs scored against actual subsequent price action (a HOLD where the price subsequently moved >5% in either direction is recorded as `expired_positive` or `expired_negative` against the neutral call). Direction mapping for actionable verdicts: BUY/ADD → long; TRIM/SELL → short; HOLD → neutral (logged, scored). Conviction → not stored on the prediction row itself but writable as `evidence_json` if needed later.

  Additionally, the `source_reliability` view exposes a **participation/coverage** column (`abstain_rate = COUNT(direction='neutral') / COUNT(*)`) so the user can see when an agent is over-using HOLD relative to peers. Consumers may penalize high-abstain-rate agents via a separate `participation_penalty` factor (TBD in commit #6).
- `argosy/services/news_analyst_runner.py` — after each `AnalyzedSignalOut`, call `write_news_signal_analyst_prediction(session, source_ref={"news_signal_id": s.id}, analyzed=s)`. Direction mapping: `recommended_flag=macro_shift` + sentiment=positive → long (basket = parsed_tickers); sentiment=negative → short; sentiment=neutral or `recommended_flag=None` → log as `unparseable_reason='no_actionable_direction'`.
- Spec B `state_observer.py` — after each flag, call `write_state_observer_prediction(session, source_ref={"flag_uuid": ...}, flag=...)`. Direction mapping is flag-specific and lives in Spec B; this spec defines only the contract that state_observer flags reach the ledger.
- `argosy/services/plan_monitor.py::check_allocation_drift` — after `MonitorFlag` insert with kind=`allocation_drift`, call `write_monitor_flag_prediction(session, source_ref={"monitor_flags.id": ...}, flag=...)`. Direction mapping: drift's allocation recommendation (BUY suggestion → long; TRIM/SELL suggestion → short).
- `argosy/services/plan_monitor.py::check_mc_regression` — `mc_regression` flags are predictions ABOUT plan health, not about ticker prices, so direction=`neutral` and `ticker=NULL`. These get scored only via the `expired_*` path on `fixed_lookahead_30d` against a portfolio-level proxy (the portfolio's USD value vs last-month's snapshot). The reliability column for `internal_monitor_flags/mc_regression` is "did P(solvent) drop predict actual portfolio drawdown?" — a meta-question, scored coarsely.

The wiring is minimal: each existing producer gains exactly one `write_*` call at its existing exit point. No producer changes its primary logic.

## Section 3 — Outcome evaluator service

File: `argosy/services/predictions/evaluator.py`. Registered with Spec A's JobRegistry as job `predictions-evaluate-due`, cadence `daily 02:00 IST`.

### Section 3.1 — Algorithm

```
1. SELECT predictions p
   WHERE p.evaluation_due_at <= today
     AND NOT EXISTS (
       SELECT 1 FROM prediction_outcomes o
       WHERE o.prediction_id = p.id
         AND o.evaluation_method = p.evaluation_method
     ).
   Limit: batch_size (default 500) per run; cursor on (id, event_at).

   NOTE (Codex BLOCKER 2 fix): the due query keys off the PRE-COMPUTED
   `evaluation_due_at` column on `predictions` (set at write time to
   `event_at + chosen_window_days`), NOT off raw `timeframe_days`. This
   ensures a 13F prediction with `timeframe_days=90` but `evaluation_method
   = fixed_lookahead_30d` becomes due at 30 days (per §5.5), not 90.

2. For each due prediction:
     - `evaluation_method` is already stored on the row (set at write
       time per §1.2). Re-derivation is NOT done at evaluation time —
       the writer is the single source of truth for method selection.
     - Fetch daily bars from price adapter for the window
         [event_at + 1 trading day, event_at + chosen_window_days].
     - Apply scoring rule (Section 5) → outcome_kind, pnl_pct.
     - Insert prediction_outcomes row.

3. Log evaluator run stats (predictions evaluated, ticker count, adapter
   errors, evaluation latency) to Spec A's job-run telemetry.
```

**Writer-side method selection (referenced from §2 — moved here for §3.1 cohesion):**
```
* If target_price AND stop_price both set    → target_stop,        window = timeframe_days
* If direction = multi                        → multi_basket_weighted, window = min(timeframe_days, 30)
* If unparseable_reason set                   → unparseable,        window = 0 (immediately due)
* Else if timeframe_days <= 7                 → fixed_lookahead_7d, window = 7
* Else if timeframe_days <= 30                → fixed_lookahead_30d, window = 30
* Else (timeframe_days > 30, e.g. 13F at 90d) → fixed_lookahead_30d, window = 30 (per §5.5)
```
`evaluation_due_at = event_at + window` is computed and stored.

### Section 3.2 — Price-data adapter usage

Adapters consulted in order:
1. `argosy/adapters/data/finnhub_adapter.py` — US-listed equities (primary).
2. `argosy/adapters/data/yfinance_adapter.py` — fallback when Finnhub returns nothing or rate-limits.
3. Future: `argosy/adapters/data/fred_adapter.py` for macro predictions on rates/CPI/jobs (e.g. news_signal_analyst predicting "Fed will cut" → FRED federal funds rate).

The evaluator calls `get_daily_bars(ticker, from_date, to_date)` returning `[{date, open, high, low, close, volume}, ...]`. Bars are fetched once per (ticker, window) per evaluator run, cached in `argosy/adapters/data/cache.py` per the existing `CacheKind` convention (24h TTL).

### Section 3.3 — Edge cases the evaluator MUST handle

These are codex-probe-worthy. Each case has a definitive resolution in Section 5; the evaluator code dispatches based on detection:

| Edge case | Detection | Resolution |
|---|---|---|
| Ticker delisted within timeframe | adapter returns bars covering only [created_at, delisting_date] | outcome=`unparseable`, pnl_pct=NULL, notes="ticker delisted on YYYY-MM-DD" |
| Ticker has no bars at all in window | adapter returns empty array | outcome=`unparseable`, pnl_pct=NULL, notes="no price data for window" |
| Window contains weekends/holidays (always true) | bars dict has gaps | Use only trading-day bars; cross-day target/stop hits use intraday high/low if available, otherwise close-only (deterministic per Section 5.3) |
| Mid-window price gap (overnight gap through target or stop) | bar's `open` is past the level the prior bar didn't reach | Counts as a hit at the gap-day's `open` price; pnl_pct uses the gap price (NOT the level itself — gap-down through stop hurts more than the stop assumed) |
| Both target and stop hit on the same day | intraday high reaches target AND intraday low reaches stop | **Determinism rule (Section 5.3 — Codex IMPORTANT 1 fix, v1):** ALWAYS treat the adverse extreme as hitting first → outcome = `hit_stop` regardless of target/stop distance. Symmetric for shorts (target up, stop down → stop wins on a same-bar touch). Distance-invariant for cross-source comparability. Replayable, conservative, deterministic. |
| Source's stated entry_price differs from adapter snapshot | the writer caught this at insert time → uses adapter snapshot; source's number is in raw_text_ref only | Evaluator uses `prediction_outcomes.entry_price_used` = adapter snapshot, never the source's stated entry |
| Adapter rate-limited or down | exception bubble | Evaluator skips this prediction this run; no row inserted; cron will retry tomorrow. Critical: do NOT write `unparseable` for transient adapter errors. |
| Prediction's timeframe expired weeks ago and we're backfilling | `created_at + timeframe_days < today - 30d` | Normal scoring; adapter cache may need historical bars. Worked example: Discord 14-day backfill in §7.1. |
| `multi` direction with mixed delisted constituents | one ticker in basket has no data | Score basket excluding the un-priceable constituent + record `notes="excluded TICKERX: no data"`. Weight redistributed proportionally to surviving constituents. |

### Section 3.4 — Determinism + replay (Codex BLOCKER 1 fix)

The evaluator is deterministic given (predictions row, price-adapter cache snapshot at evaluation time). Same inputs → same `prediction_outcomes` row.

**Method registry, not CHECK enum.** Codex BLOCKER 1 surfaced a contradiction in the original draft: §5.6 promised "add new method values" while Appendix A pinned `evaluation_method` to a fixed CHECK enum. Resolution:

- New table `evaluation_method_registry (method TEXT PRIMARY KEY, version INTEGER, family TEXT, is_active BOOLEAN, superseded_by TEXT NULL, created_at DATETIME)`.
- `predictions.evaluation_method` and `prediction_outcomes.evaluation_method` carry a FOREIGN KEY (or in SQLite, a CHECK-via-trigger that consults the registry). NO hard-coded enum on the row tables.
- Adding `fixed_lookahead_30d_v2`: INSERT one row into `evaluation_method_registry`, no schema migration needed. The OLD row (`fixed_lookahead_30d`) sets `is_active=FALSE` and `superseded_by='fixed_lookahead_30d_v2'`.

**View picks ONE method per prediction (Codex BLOCKER 1 also fixed here).** The original view used `evaluation_method IN (active_evaluation_methods)`, which double-counts when more than one row exists per prediction during a transition window. Replacement logic:

```sql
-- In the source_reliability_v1 view: pick the active method per (source, family).
JOIN evaluation_method_registry r
  ON r.method = o.evaluation_method
 AND r.is_active = TRUE
-- AND ensure exactly one row per prediction by selecting the most-recent
-- active method per family, NOT the union of all active methods.
WHERE o.id = (
  SELECT o2.id FROM prediction_outcomes o2
  JOIN evaluation_method_registry r2
    ON r2.method = o2.evaluation_method
  WHERE o2.prediction_id = o.prediction_id
    AND r2.is_active = TRUE
    AND r2.family = r.family
  ORDER BY o2.evaluated_at DESC
  LIMIT 1
)
```

This guarantees ONE row per prediction enters the aggregation regardless of how many `is_active=TRUE` method versions exist for that family. Replay no longer inflates sample size.

If Ariel changes a scoring rule parameter (e.g. switches from `fixed_lookahead_30d` to `fixed_lookahead_30d_v2`), the evaluator is run with `replay=True` over the affected predictions, which: (a) inserts new `prediction_outcomes` rows under the new `evaluation_method` discriminator, (b) DOES NOT modify existing rows, (c) registry flip — old method `is_active=FALSE`, new method `is_active=TRUE`, both rows preserved. The view automatically picks the new rows on the next query.

The codex-probe-worthy claim: **same predictions row + same price-adapter daily bars → same outcome row every replay; view never double-counts a single prediction across method versions.** No randomness, no agent calls, no LLM in the evaluator.

## Section 4 — `source_reliability` view + service

File: `argosy/services/predictions/reliability.py`. Backed by SQL view `source_reliability_v1`.

### Section 4.1 — SQL view (PostgreSQL syntax; SQLite analog in the migration)

```sql
CREATE VIEW source_reliability_v1 AS
SELECT
  p.source,
  COUNT(*)                                      AS sample_size,
  COUNT(*) FILTER (WHERE o.outcome_kind != 'unparseable')
                                                AS sample_size_scored,
  COUNT(*) FILTER (WHERE o.outcome_kind = 'unparseable')
                                                AS sample_size_unparseable,
  -- Hit rate: outcome confirms predicted direction
  AVG(CASE
        WHEN o.outcome_kind = 'unparseable' THEN NULL
        WHEN o.outcome_kind = 'hit_target' THEN 1.0
        WHEN o.outcome_kind = 'hit_stop'   THEN 0.0
        WHEN o.outcome_kind = 'expired_positive' AND p.direction = 'long' THEN 1.0
        WHEN o.outcome_kind = 'expired_negative' AND p.direction = 'short' THEN 1.0
        WHEN o.outcome_kind = 'expired_neutral' THEN 0.5  -- a wash
        ELSE 0.0
      END)                                      AS hit_rate,
  -- Average P/L per call (signed against the prediction's direction)
  AVG(CASE
        WHEN o.outcome_kind = 'unparseable' THEN NULL
        WHEN p.direction = 'short' THEN -o.pnl_pct  -- short profits on negative price move
        ELSE o.pnl_pct
      END)                                      AS avg_pnl_pct,
  -- Coverage (% of predictions actually scoreable)
  CAST(COUNT(*) FILTER (WHERE o.outcome_kind != 'unparseable') AS NUMERIC)
    / NULLIF(COUNT(*), 0)                       AS coverage_pct,
  MAX(o.evaluated_at)                           AS last_evaluated_at,
  MIN(p.created_at)                             AS first_prediction_at,
  MAX(p.created_at)                             AS last_prediction_at
FROM predictions p
JOIN prediction_outcomes o
  ON o.prediction_id = p.id
JOIN evaluation_method_registry r
  ON r.method = o.evaluation_method
 AND r.is_active = TRUE
-- Codex BLOCKER 1 fix: ONE outcome per prediction even if multiple
-- active method versions exist for the same family. Pick the most-
-- recently-evaluated active row per (prediction_id, family).
WHERE o.id = (
  SELECT o2.id FROM prediction_outcomes o2
  JOIN evaluation_method_registry r2
    ON r2.method = o2.evaluation_method
  WHERE o2.prediction_id = o.prediction_id
    AND r2.is_active = TRUE
    AND r2.family = r.family
  ORDER BY o2.evaluated_at DESC
  LIMIT 1
)
AND p.event_at >= NOW() - INTERVAL '365 days'
GROUP BY p.source;
```

Plus a rolling-window variant:

```sql
CREATE VIEW source_reliability_rolling_v1 AS
SELECT
  p.source,
  -- Rolling N-day windows
  COUNT(*) FILTER (WHERE p.created_at >= NOW() - INTERVAL  '7 days')  AS n_7d,
  COUNT(*) FILTER (WHERE p.created_at >= NOW() - INTERVAL '30 days')  AS n_30d,
  COUNT(*) FILTER (WHERE p.created_at >= NOW() - INTERVAL '90 days')  AS n_90d,
  AVG(...) FILTER (WHERE p.created_at >= NOW() - INTERVAL  '7 days')  AS hit_rate_7d,
  AVG(...) FILTER (WHERE p.created_at >= NOW() - INTERVAL '30 days')  AS hit_rate_30d,
  AVG(...) FILTER (WHERE p.created_at >= NOW() - INTERVAL '90 days')  AS hit_rate_90d,
  ...
FROM predictions p
JOIN prediction_outcomes o ON ... ;
```

SQLite doesn't support `FILTER (WHERE ...)` but supports `CASE WHEN ... END` equivalents; migration 0049 ships both flavors via `op.execute(dialect-conditional SQL)`.

### Section 4.2 — Python service accessor

```python
@dataclass(frozen=True)
class SourceReliability:
    source: str                  # the enum value
    sample_size: int
    sample_size_scored: int
    sample_size_unparseable: int
    hit_rate: float | None        # None if sample_size_scored == 0
    avg_pnl_pct: float | None     # None if sample_size_scored == 0
    coverage_pct: float           # 0..1
    last_evaluated_at: datetime | None
    last_prediction_at: datetime | None

def get_source_reliability(
    session: Session,
    source: PredictionSource,
    *,
    window: Literal["all", "7d", "30d", "90d"] = "30d",
) -> SourceReliability:
    """Returns reliability metrics for the given source. Cached in-memory
    with 5-min TTL (consumers call this on every weight decision; recompute
    less often than that)."""
```

### Section 4.3 — The "small sample" problem — minimum-sample-size for weighting

A source with 2 predictions and a 100% hit rate is NOT 2× as accurate as a source with 200 predictions and 50% hit rate. To prevent two-sample reliability from dominating the weighting, consumers MUST apply a confidence floor:

```python
def effective_weight(reliability: SourceReliability,
                     prior_weight: float = 1.0,
                     min_samples: int = 20) -> float:
    """Return the weight to apply to this source's signals.
    If sample_size_scored < min_samples, return prior_weight (no adjustment).
    Otherwise: weight = prior_weight * (hit_rate / 0.5).
    Clipped to [0.25, 4.0] to prevent runaway up- or down-weighting."""
```

The `min_samples=20` threshold + clipping range are tunable per consumer + recorded in `argosy/services/predictions/reliability.py`. **Both are codex-probe-worthy parameters** — Section 6's anti-feedback-loop discussion uses them.

## Section 5 — Scoring rule (CODE-LEVEL precision)

This is the most codex-probe-worthy section. Same input → same outcome, deterministically.

### Section 5.1 — Method `target_stop` (when both target + stop set)

**Applies when:** `predictions.target_price IS NOT NULL AND predictions.stop_price IS NOT NULL`.

**Algorithm:**

```python
def score_target_stop(p: Prediction, bars: list[Bar]) -> Outcome:
    """bars: daily bars for [p.created_at + 1 trading day, p.created_at +
    p.timeframe_days]. Empty bars → unparseable."""
    if not bars:
        return Outcome(kind="unparseable", notes="no price data")

    entry = p.entry_price  # set at write time by adapter snapshot
    target, stop = p.target_price, p.stop_price
    direction = p.direction  # 'long' or 'short'

    target_hit_date, stop_hit_date = None, None
    target_hit_price, stop_hit_price = None, None

    for bar in bars:
        target_hit_this_bar = (
            (direction == "long" and bar.high >= target) or
            (direction == "short" and bar.low <= target)
        )
        stop_hit_this_bar = (
            (direction == "long" and bar.low <= stop) or
            (direction == "short" and bar.high >= stop)
        )

        if target_hit_this_bar and stop_hit_this_bar:
            # Codex IMPORTANT 1 fix: ALWAYS adverse-first (stop wins) when
            # both extremes touched on the same bar — distance-invariant,
            # symmetric across long/short, deterministic, conservative.
            # See §5.3 for the rationale (cross-source comparability).
            stop_hit_date, stop_hit_price = bar.date, stop
            target_hit_date = None
            break

        if target_hit_this_bar:
            # Gap-through detection: if the bar's open already exceeded
            # the target, exit price is the open (gap favorable).
            if (direction == "long" and bar.open >= target) or \
               (direction == "short" and bar.open <= target):
                target_hit_price = bar.open
            else:
                target_hit_price = target  # touched intra-bar
            target_hit_date = bar.date
            break

        if stop_hit_this_bar:
            # Gap-through detection: gap-down through stop = exit at open.
            if (direction == "long" and bar.open <= stop) or \
               (direction == "short" and bar.open >= stop):
                stop_hit_price = bar.open  # gap-down hurts: worse than stop
            else:
                stop_hit_price = stop  # touched intra-bar
            stop_hit_date = bar.date
            break

    if target_hit_date:
        pnl_pct = (target_hit_price - entry) / entry
        if direction == "short":
            pnl_pct = -pnl_pct
        return Outcome(
            kind="hit_target",
            pnl_pct=pnl_pct,
            exit_price_used=target_hit_price,
            exit_trigger_date=target_hit_date,
        )
    if stop_hit_date:
        pnl_pct = (stop_hit_price - entry) / entry
        if direction == "short":
            pnl_pct = -pnl_pct
        return Outcome(
            kind="hit_stop",
            pnl_pct=pnl_pct,
            exit_price_used=stop_hit_price,
            exit_trigger_date=stop_hit_date,
        )

    # Neither hit. Classify end-of-window expiration by sign.
    last_close = bars[-1].close
    raw_return = (last_close - entry) / entry
    signed_pnl = raw_return if direction == "long" else -raw_return
    if abs(signed_pnl) < 0.01:  # within ±1% — a wash
        kind = "expired_neutral"
    elif signed_pnl > 0:
        kind = "expired_positive"
    else:
        kind = "expired_negative"
    return Outcome(
        kind=kind,
        pnl_pct=signed_pnl,
        exit_price_used=last_close,
        exit_trigger_date=bars[-1].date,
    )
```

### Section 5.2 — Method `fixed_lookahead_7d` / `fixed_lookahead_30d`

**Applies when:** `target_price` and/or `stop_price` NULL (most predictions). Scored at the fixed lookahead window's end, classifying by sign of return.

**Algorithm:**

```python
def score_fixed_lookahead(p: Prediction, bars: list[Bar], window_days: int) -> Outcome:
    if not bars:
        return Outcome(kind="unparseable", notes="no price data")
    entry = p.entry_price
    last_close = bars[-1].close  # closes nearest to created_at + window_days
    raw_return = (last_close - entry) / entry
    signed_pnl = raw_return if p.direction == "long" else -raw_return

    # Even without an explicit target/stop, mark "large favorable move"
    # as hit_target-equivalent for reliability stats. Threshold chosen
    # so a "long" call that delivers +10% in 7d counts as a hit, not a
    # mild expired_positive.
    if signed_pnl >= 0.10:
        kind = "hit_target"
    elif signed_pnl <= -0.10:
        kind = "hit_stop"
    elif abs(signed_pnl) < 0.01:
        kind = "expired_neutral"
    elif signed_pnl > 0:
        kind = "expired_positive"
    else:
        kind = "expired_negative"
    return Outcome(
        kind=kind,
        pnl_pct=signed_pnl,
        exit_price_used=last_close,
        exit_trigger_date=bars[-1].date,
    )
```

The 10% / 1% thresholds are tuneable in `predictions/scoring_config.py`. **Tunable but locked per `evaluation_method`** — changing them creates a new `evaluation_method` value (e.g. `fixed_lookahead_30d_v2`) per Section 3.4's replay contract.

### Section 5.3 — Same-day target + stop determinism rule

Recap from §3.3: when intraday high reaches target AND intraday low reaches stop on the same bar, we don't know the trajectory within the day.

**Codex IMPORTANT 1 fix — switched to symmetric pessimistic-first rule.** The original "stop closer to entry → stop wins, else target wins" rule was distance-conditional and arbitrarily punished wide-target setups. Replacement (v1): **always adverse-first** — when both target and stop are touched on the same bar, the outcome is `hit_stop` regardless of distance. This is:

- **Deterministic** (same prediction + same bar → same outcome, always).
- **Conservative** (never optimistically resolves ambiguity in favor of the prediction).
- **Symmetric** across long and short (the "adverse" extreme = the one that hurts the prediction's direction).
- **Distance-independent** (no synthetic intraday trajectory assumption; doesn't bias against wide-target setups).

This rule lives under `evaluation_method = target_stop` (the v1 method). If we later want to A/B against a distance-conditional rule, that ships as `target_stop_v2` in `evaluation_method_registry` per §3.4 — no historical rows touched.

(Earlier draft's "stop closer → stop wins, target closer → target wins" rule is preserved below as the rejected alternative for posterity. The decisive argument: cross-source comparability requires distance-INVARIANT scoring, otherwise high-volatility sources with wide stops get systematically punished vs tight-stop sources.)

Worked example (v1 rule — adverse-first):
- Long NVDA at entry=145, target=180, stop=135.
- On day 3, bar = {open: 144, high: 182, low: 133, close: 138}.
- Both target (182 ≥ 180) and stop (133 ≤ 135) hit → adverse-first → outcome = `hit_stop`, exit_price=135, pnl_pct=-6.9%.

Inverse case (same v1 rule):
- Long NVDA at entry=145, target=150, stop=120.
- On day 3, bar = {open: 144, high: 151, low: 118, close: 122}.
- Both hit → adverse-first → outcome = `hit_stop`, exit_price=120, pnl_pct=-17.2%.

Short symmetry:
- Short NVDA at entry=145, target=130, stop=160.
- Day 3 bar = {open: 146, high: 162, low: 129, close: 145}.
- Both hit (low ≤ 130 = target hit for short; high ≥ 160 = stop hit for short). Adverse-first → `hit_stop`, exit_price=160, pnl_pct (signed for short) = -(160 - 145)/145 = -10.3%.

The rule is symmetric across long and short by construction. Distance-invariant, distance-independent, distance-blind — all three.

### Section 5.4 — Method `multi_basket_weighted`

**Applies when:** `predictions.direction = 'multi'`.

**Algorithm:**

```python
def score_multi_basket(p: Prediction, bars_by_ticker: dict[str, list[Bar]]) -> Outcome:
    basket = json.loads(p.multi_ticker_json)  # list of {ticker, direction, weight}
    pnl_components = []
    total_weight = 0.0
    notes_parts = []
    for item in basket:
        ticker = item["ticker"]
        direction = item["direction"]
        weight = item["weight"]
        bars = bars_by_ticker.get(ticker, [])
        if not bars:
            notes_parts.append(f"excluded {ticker}: no data")
            continue
        # Use entry_price per-ticker — fetched at write time via the
        # writer's per-ticker snapshot.
        entry = p.entry_prices_json[ticker]  # multi-ticker snapshot map
        last_close = bars[-1].close
        raw_return = (last_close - entry) / entry
        signed = raw_return if direction == "long" else -raw_return
        pnl_components.append(signed * weight)
        total_weight += weight
    if total_weight == 0:
        return Outcome(kind="unparseable", notes="; ".join(notes_parts))
    # Renormalize by total surviving weight
    weighted_pnl = sum(pnl_components) / total_weight
    # Classification like fixed_lookahead
    if weighted_pnl >= 0.10:
        kind = "hit_target"
    elif weighted_pnl <= -0.10:
        kind = "hit_stop"
    elif abs(weighted_pnl) < 0.01:
        kind = "expired_neutral"
    elif weighted_pnl > 0:
        kind = "expired_positive"
    else:
        kind = "expired_negative"
    return Outcome(
        kind=kind, pnl_pct=weighted_pnl,
        notes="; ".join(notes_parts) if notes_parts else None,
    )
```

Note `entry_prices_json` is a new JSON column added in migration 0048 for multi-ticker predictions — writers snapshot all constituents at write time.

### Section 5.5 — Why fixed-lookahead-30d for predictions with longer stated timeframes

A 13F filing implies a 90-day-ish hold. A state-observer concentration flag may be a multi-month thesis. Why don't we wait the full stated timeframe before scoring?

Three reasons:
1. **Replay speed.** A 90-day window means a single prediction can't be evaluated until 90 days after the event. The Discord backfill case (§7) wants 14-day-back data scoreable today.
2. **Decision latency.** Source-reliability has to feed back to consumers within weeks, not quarters. Otherwise the system can't adapt to a source going stale.
3. **Information content.** The first 30 days of price action after a 13F filing or a concentration flag captures most of the immediate signal-vs-noise question. The remaining 60 days of a 90-day call adds noise more than information.

That said, this is a knob. The `evaluation_method_registry` (per §3.4 Codex BLOCKER 1 fix) is the override point — toggle `is_active` per method-version row. Future work can A/B `fixed_lookahead_30d` vs `fixed_lookahead_90d` for 13F predictions and pick the one with better hit-rate-stability.

### Section 5.6 — Replay semantics (re-iterating §3.4)

Existing `prediction_outcomes` rows are immutable. If the scoring rule changes, the new evaluation method is run as a parallel evaluator pass, inserting new outcome rows under the new `evaluation_method` discriminator. The view's `evaluation_method_registry.is_active` filter (per §3.4 Codex BLOCKER 1 fix) picks which method's rows count toward reliability — a registry table, not code-baked, and not a hard-coded enum.

The migration ships with `evaluation_method_registry` seeded with all five v1 values (each `is_active=TRUE`); future operators can swap in alternate method versions by INSERTing a new row + flipping `is_active` flags, without touching ledger code or schema.

## Section 6 — Consumer integration (where the weights apply)

The reliability view is useless without consumers reading it. This section maps every consumer + its specific weight-application point. **Codex review will probe each one for double-counting + feedback-loop risk.**

### Section 6.1 — `synthesizer` (`argosy/agents/plan_synthesizer.py` — TBD file)

Reads source_reliability for: `internal_news_signal_analyst`, `internal_per_position_thesis`, `internal_state_observer`, `discord`, `news`. Applies weights when ranking input theses into the plan-synthesis context.

**Mechanism:** the synthesizer's plan-context preamble includes a per-source weight banner:

```
Source reliability (last 30d):
  internal_per_position_thesis:  62% hit_rate, n=18  → weight 1.24×
  internal_news_signal_analyst:  41% hit_rate, n=34  → weight 0.82×
  discord:                       47% hit_rate, n=22  → weight 0.94×
  internal_state_observer:       (n=5 — insufficient sample, weight=1.00×)

Weight signals from each source proportionally when forming the plan.
```

**Where in code:** `argosy/agents/plan_synthesizer.py` (file expected to exist or be added by Spec D — coordinated with Spec D's `/plan` cashflow redesign). The preamble lines are *not* the agent's only mechanism — they tell the LLM the weights, but the synthesizer also re-ranks structured signal inputs (`PositionThesis` list, `AnalyzedSignalOut` list) by `effective_weight()` before passing them.

### Section 6.2 — `news_signal_analyst` (`argosy/agents/news_signal_analyst.py`)

Reads source_reliability for the source of each input `NewsSignal`. Down-weights low-reliability sources before deciding `materiality`.

**Mechanism:** the agent's prompt receives a per-input `source_reliability_factor: float` alongside `source_trust`. The prompt instructs:

```
For each signal, take source_reliability_factor into account when
classifying materiality. A signal from a source with reliability_factor
< 0.7 should rarely cross to materiality='high' on sentiment alone.
```

**Where in code:** `argosy/agents/news_signal_analyst.py::analyze_batch` — augment each `AnalyzedSignalIn` with `source_reliability_factor` from `get_source_reliability(session, signal.source).effective_weight()`. The Pydantic schema extension lands in commit #6.

### Section 6.3 — `per_position_thesis` (`argosy/services/per_position_thesis.py`)

Reads source_reliability for `internal_news_signal_analyst` when consuming its signals as inputs to the per-position thesis derivation.

**Mechanism:** if `news_signal_analyst`'s reliability for the last 30d is < 0.7 effective weight, the per-position derivation downweights its sentiment input by that factor when blending with horizon-target data. Conservative tilt: a low-reliability sentiment doesn't get to *flip* a HOLD to BUY/SELL alone.

**Where in code:** `argosy/services/per_position_thesis.py::build_position_theses` — the existing function reads `agent_reports.response_text` from synthesis runs to extract cited sources. Augment the extraction step with a `weight_by_source_reliability(source, signal_strength)` filter.

### Section 6.4 — Spec B `state_observer`

Reads source_reliability for `internal_state_observer` itself (yes, the observer reads its OWN reliability to calibrate its firing threshold).

**Mechanism:** if the observer's last-30d hit-rate is < 50%, raise the firing threshold (require stronger evidence before emitting a flag). If > 70%, lower the threshold (let weaker signals fire). The implementation lives in Spec B; this spec defines only the contract: `state_observer.py` reads `get_source_reliability(session, "internal_state_observer", window="30d")` once per run.

### Section 6.5 — `plan_monitor` (`argosy/services/plan_monitor.py`)

Reads source_reliability for `internal_monitor_flags` to calibrate the drift hysteresis. If recent drift flags didn't predict actual portfolio drawdown (`mc_regression` true 30 days later), the persistent-threshold lifts from 10% to 12-15% — fewer false positives.

**Mechanism:** drift threshold parameters become a function of monitor-flag reliability:

```python
persistent_threshold = base * (1.0 if reliability.hit_rate >= 0.6
                                else 1.0 + 0.1 * (0.6 - reliability.hit_rate))
```

Clamped to `[base, 1.5 * base]`.

### Section 6.6 — Anti-feedback-loop contract (CRITICAL — codex-probe)

The risk: consumer downweights source A → fewer A signals reach material-classification → A gets less *exposure* in the ledger → A's hit-rate goes more volatile → A gets downweighted further. Death spiral.

Mitigations:

1. **Reliability uses prediction count, NOT consumer-action count.** A source's signals are written to the ledger regardless of consumer weights. A downweighted Discord source still gets every message logged as a prediction; only the *downstream consumption* is reduced. So the denominator stays intact.

2. **Minimum-sample floor.** `effective_weight()` returns 1.0 (no adjustment) when `sample_size_scored < 20`. So even a downweighted source can't drop below 0.25× unless we have ≥20 outcomes to be confident about.

3. **Asymmetric clipping.** `effective_weight()` clips to `[0.25, 4.0]`. A source can be downweighted to a quarter but not zeroed; some signal still flows.

4. **Rolling window resets stale judgement.** The reliability view uses `last 30 days` (or `last 90 days`) windows — a source that performed badly 6 months ago but is recovering won't be perma-banished. Recovery is detectable within a month.

5. **Manual override.** `argosy/services/predictions/reliability.py::set_manual_weight(source, weight, expires_at)` lets the user override the computed weight (e.g. "I trust 13F filings during macro panic — manual weight 2.0× for the next 30 days"). The setting is logged.

6. **Provenance-aware weighting (Codex IMPORTANT 3 fix — anti-double-application).** The risk Codex surfaced: discord gets downweighted at `news_signal_analyst`, then the resulting `internal_news_signal_analyst` output is downweighted AGAIN at the synthesizer, compounding the discount. Mitigation contract:

   - Every consumer that applies a reliability weight stamps the prediction-derivative (`AnalyzedSignal`, `PositionThesis`, plan_synthesizer input row, etc.) with a `provenance_weights_applied: dict[str, float]` field containing `{source: applied_weight}` for every source already discounted upstream.
   - Downstream consumers consult this field and SKIP re-applying any source already in the dict (idempotent weighting).
   - Each derivative also stamps `cumulative_attenuation: float = product(provenance_weights_applied.values())` clipped to a floor of 0.10 (no more than 10× total dimming end-to-end). The clip is the safety net if a future consumer forgets to consult `provenance_weights_applied`.
   - Tests in commit #6: feed a Discord signal through `news_signal_analyst` (0.5× discord weight applied) → into `plan_synthesizer` → verify synthesizer does NOT re-multiply by 0.5×; verify `cumulative_attenuation` stays at 0.5× end-to-end.

The codex-probe-worthy claim: **a downweighted source still gets observed; only consumption is dimmed; recovery is detectable; manual override exists; each source's discount is applied at most once per derivative path.**

### Section 6.7 — Consumer-integration table (citation-style summary)

| Consumer | File:approximate-line | Reads reliability for | Applies how |
|---|---|---|---|
| synthesizer | `argosy/agents/plan_synthesizer.py` (TBD per Spec D) | `internal_news_signal_analyst`, `internal_per_position_thesis`, `internal_state_observer`, `discord`, `news` | Preamble weights + structured input re-ranking |
| news_signal_analyst | `argosy/agents/news_signal_analyst.py::analyze_batch` (~L100) | source of each input `NewsSignal` | Per-input `source_reliability_factor` injected into prompt + influences materiality threshold |
| per_position_thesis | `argosy/services/per_position_thesis.py::build_position_theses` (~L150) | `internal_news_signal_analyst` | Blending weight on sentiment input |
| state_observer (Spec B) | Spec B's `state_observer.py` | `internal_state_observer` (self) | Firing-threshold calibration |
| plan_monitor | `argosy/services/plan_monitor.py::check_allocation_drift` (~L80) | `internal_monitor_flags` | Drift-hysteresis threshold inflation |

## Section 7 — Discord 14-day backfill (worked example)

This is the user's original ask, reframed as the first concrete use of the general system.

### Section 7.1 — What lands

Sprint commit #7 ships `argosy/services/predictions/discord_backfill.py` registered with Spec A's JobRegistry as `predictions-backfill-discord` (manual trigger only — not scheduled).

Trigger: `POST /api/jobs/predictions-backfill-discord/run-now` (Spec A's job-run-now route).

Assumptions:
- Spec #1 commit #16's Discord listener is registered as a Spec-A job (`discord-listener`) and may already be running. If not, the backfill runs in standalone mode reading creds the same way (`~/.argosy/discord_creds.json`).
- The user has read-rights on the channel.
- 14 days = ~hundreds of messages, not tens of thousands.

### Section 7.2 — Backfill algorithm

```
1. Load creds from ~/.argosy/discord_creds.json.
   If missing → 422 error to API caller; no-op.

2. Connect to Discord gateway with REST fetch:
   GET /channels/{channel_id}/messages?limit=100 (paginated).
   Walk backwards from now to (now - 14d).

3. For each message:
     - Skip if dedup_key
       (`v1|predictions|discord|{channel_id}.{message_id}`) already in
       predictions.message_id. Idempotent.
     - Feed message body to news_extractor.extract(text, source="discord").
       This is the same Stage 1 extractor used by the live listener —
       writes a news_signals row + returns the ExtractedSignal.
     - Call write_discord_prediction(session, source_ref={"channel_id":...,
       "message_id":...}, extracted=signal). The writer:
         * If extracted has ticker + (target or stop or strong direction
           keyword): create a structured prediction.
         * Otherwise: insert with unparseable_reason='no_actionable_call'.
     - In both cases: take adapter snapshot of entry_price at message timestamp
       (NOT at backfill-run time). Hindsight-bias killer per §2.3.

4. Log progress to Spec A's job-run telemetry.

5. After backfill completes, trigger one immediate run of
   predictions-evaluate-due. Since all backfilled predictions are
   ≥timeframe-days-old by definition (we backfilled history), they
   are all immediately scoreable.

6. The user can then visit /admin/source-reliability (commit #8 UI)
   and see Discord's 14-day reliability.
```

### Section 7.3 — Discord-specific parsing notes

The Discord channel format Ariel watches uses informal conventions like `$NVDA → $180 stop $135 by Fri`. The Stage-1 extractor's regex tuning lives in `news_extractor.py`; the backfill DOES NOT add Discord-specific extraction logic — it uses the same extractor as live ingestion. Anything the live extractor misses, the backfill misses too. This is intentional: backfill reliability == live reliability, so reliability score is honest.

If the live extractor is found inadequate after the backfill (e.g. <50% of messages are parseable as calls), a separate spec adds a `discord_call_parser.py` LLM-assisted extractor — out of scope for this spec.

## Section 8 — Optional UI commit — `/admin/source-reliability`

If commit time permits, ship a small admin page at `/admin/source-reliability` showing:

- **Leaderboard table** — one row per source, columns: source, sample_size (last 30d), hit_rate, avg_pnl_pct, coverage_pct, last_evaluated_at, effective weight (1.00× = baseline).
- **Drill-down** — click a source → modal showing last 20 predictions with outcomes + chart of rolling hit-rate over the last 90 days.
- **Manual override panel** — set `set_manual_weight(source, weight, expires_at)` from the UI.

This is **purely visualization** — no logic, no scoring rules, no consumer wiring. If the sprint is over commit budget, this lands in a follow-on UI spec.

## Section 9 — Risk register

| Risk | Mitigation |
|---|---|
| Scoring rule favors high-conviction sources (those with explicit target+stop) over lower-conviction sources by accident | Same outcome enum applies to both methods; `expired_*` and `hit_*` are both scoreable. Coverage is reported separately so a low-conviction high-coverage source is distinguishable from a high-conviction low-coverage one. |
| Ticker delistings, splits, mergers corrupt entry/exit price math | Evaluator detects empty/short bar sequences → `unparseable`. Future spec: split-adjustment handling via Finnhub `stock_splits` endpoint. |
| Price-adapter cache staleness biases scoring | Cache TTL = 24h; the evaluator runs at 02:00 IST after market close. Adapter call resilience: yfinance fallback on Finnhub failure. |
| Ledger row growth — millions of predictions/year | Discord ~50 msg/day = 18k/year. News ~200 items/day = 73k/year. Internal agents add ~30 predictions/day = 11k/year. Total ~100k/year. SQLite handles 10M rows fine; PostgreSQL handles 100M. Retention operationalized in §9.1 (Codex IMPORTANT 4 fix). |
| Hindsight bias via wrong entry_price | Writer takes snapshot at message timestamp using adapter's historical close. Snapshot stored on row; never re-fetched. (Section 2.3.) |
| Feedback loop: consumer downweight → source dim → score volatile → further downweight | Five-mitigation suite in §6.6: signals always logged (denominator intact), min-sample floor, clipping, rolling window, manual override. |
| Internal agents scored against themselves create incentive misalignment | Acceptable: per Ariel's binding decision, the system measures its own reliability. The scoring rule is shared with external sources, so internal can't game the score without becoming a better external source equivalent. |
| Evaluator non-idempotency on retry | The UNIQUE `(prediction_id, evaluation_method)` index forces idempotency at the DB. Evaluator uses `ON CONFLICT DO NOTHING`. |
| Discord backfill duplicates live-listener entries | The `(source, message_id)` UNIQUE index dedupes regardless of which writer inserts first. |
| Spec dependency on Spec A's JobRegistry (not yet shipped) | Commits #4 and #7 register jobs with whatever job system exists at commit time. If Spec A is incomplete, fallback: register via cron entry in `argosy/scripts/cron.sh` (existing convention) and migrate later. |
| Spec dependency on Spec B's state_observer (not yet shipped) | Commit #3's state_observer writer is a stub if Spec B hasn't shipped. Writer call site is `pass` until Spec B's observer exists; no breakage. |

### Section 9.1 — Retention policy operationalization (Codex IMPORTANT 4 fix)

Codex flagged that the original retention claim ("keep raw forever, archive evidence_json after 1 year") was prose-only with no enforcement mechanism. v1 ships a concrete retention job:

**Job:** `predictions-retention-compact`, registered with Spec A's JobRegistry, cadence `weekly Sunday 03:00 IST` (well after the daily evaluator).

**Behavior:**
1. For each `prediction_outcomes` row with `evaluated_at < NOW() - INTERVAL '365 days'`:
   - Replace `evidence_json` with a compact summary: `{"compacted_at": <timestamp>, "n_bars_orig": <n>, "first_bar": <bar>, "trigger_bar": <bar>, "last_bar": <bar>}` (3 bars + metadata, instead of N daily bars).
   - The full original blob is moved to a cold-store path under `${ARGOSY_DATA_ROOT}/evidence_archive/<year>/<prediction_id>.json.zst` (zstd-compressed).
2. For each `predictions` row with `event_at < NOW() - INTERVAL '730 days'` (2 years) AND no `prediction_outcomes` row referencing it from the last 90 days:
   - Marked `archived=TRUE` (new boolean column). Excluded from `source_reliability_v1` view by default. Still queryable via `source_reliability_archive_v1`.

**Storage budget:** with the 3-bar compact summary, `prediction_outcomes` rows compress from ~10-30 KB to ~1 KB each. Cold-store archive grows ~1 GB/year at projected volume — manageable on a desktop home directory or cloud bucket. Compact summary is sufficient to recompute hit-rate from the row alone; full-bar replay requires fetching the archive.

**Tests:** in commit #4 we add a test that runs the retention job against a synthetic 2-year-old set, verifies compact summaries are written + archive files exist + view results unchanged (since aggregations don't read `evidence_json`).

**Migration:** the retention job lands as part of commit #4 (evaluator), not a follow-on. Codex IMPORTANT 4 is closed.

## Section 10 — Sprint commit details

### Commit #1 — Migration 0048 `predictions` table

- New migration file `alembic/versions/0048_predictions.py`.
- Adds `predictions` table (DDL in Appendix A).
- Adds `PredictionSource` Python enum + `PredictionDirection` enum + ORM model in `argosy/state/models.py`.
- Adds `evaluation_method_registry` table (Codex BLOCKER 1 fix — replaces the removed `active_evaluation_methods` from the original draft; used by reliability view).
- Tests: migration up/downgrade clean; CHECK constraints fire on bad values; UNIQUE index on (source, message_id) prevents dedup violation.
- Codex zigzag: schema flexibility (§1.4 worked examples), index plan, enum extensibility.

### Commit #2 — Migration 0049 `prediction_outcomes` table

- New migration file `alembic/versions/0049_prediction_outcomes.py`.
- Adds `prediction_outcomes` table (DDL in Appendix A).
- Adds `PredictionOutcomeKind` + `PredictionEvaluationMethod` enums + ORM model.
- Adds `source_reliability_v1` + `source_reliability_rolling_v1` views (dialect-conditional SQL).
- Seeds `evaluation_method_registry` with all five v1 values (per Codex BLOCKER 1 — registry replaces enum CHECK on outcome row).
- Tests: replay semantics — same prediction can have multiple outcome rows under different methods; UNIQUE on (prediction_id, method) prevents same-method duplicates.
- Codex zigzag: outcome enum, scoring-method enum, replay design, view correctness.

### Commit #3 — Writer adapters + call-site wiring

- New file `argosy/services/predictions/writers.py` with 8 writer functions (5 active + 3 stubs).
- New file `argosy/services/predictions/__init__.py`.
- Call-site additions:
  - `argosy/services/discord_listener.py` — call `write_discord_prediction` after `news_extractor.extract` (one-line addition).
  - `argosy/services/news_extractor.py` — call `write_news_prediction` at end of `extract` (one-line addition).
  - `argosy/services/news_analyst_runner.py` — call `write_news_signal_analyst_prediction` per AnalyzedSignalOut (one-line addition).
  - `argosy/services/per_position_thesis.py::build_position_theses` — loop-end addition (~3 lines).
  - `argosy/services/plan_monitor.py::check_allocation_drift` + `check_mc_regression` — after MonitorFlag insert (one-line addition each).
  - Spec B's `state_observer.py` — stub call (no-op if Spec B not shipped).
- Tests: per-source writer idempotency; dedup-key formulas exact match; hindsight-bias canary (writer NEVER reads a price newer than `created_at`).
- Codex zigzag: idempotency contract, hindsight-bias contract, per-source dedup formula stability across re-ingests.

### Commit #4 — Outcome evaluator service

- New file `argosy/services/predictions/evaluator.py` with the algorithm of §3.
- New file `argosy/services/predictions/scoring.py` with the four scoring functions of §5 + the determinism rule of §5.3.
- Registered with Spec A as job `predictions-evaluate-due` cadence `daily 02:00 IST`.
- Tests: every edge case in §3.3 (delisting, weekends, gap, same-day target+stop, multi missing constituent); determinism — same inputs → same outputs across two runs; replay — running evaluator twice on same prediction inserts only one outcome row.
- Codex zigzag: scoring-rule determinism (§5.3 worked example replication), price-adapter edge cases, replay semantics.

### Commit #5 — `source_reliability` view + service

- View SQL migration (separate file or amendment of commit #2's migration — TBD).
- New file `argosy/services/predictions/reliability.py` with `get_source_reliability`, `effective_weight`, `set_manual_weight`.
- Tests: rolling-window correctness (90d / 30d / 7d); minimum-sample floor returns 1.0×; clipping to [0.25, 4.0]; manual override.
- Codex zigzag: SQL view correctness, min-sample floor + clipping behavior under low samples.

### Commit #6 — Consumer integration

- Touches:
  - `argosy/agents/news_signal_analyst.py` — adds `source_reliability_factor` to `AnalyzedSignalIn` schema + prompt augmentation per §6.2.
  - `argosy/services/per_position_thesis.py::build_position_theses` — adds reliability-blending per §6.3.
  - `argosy/services/plan_monitor.py::check_allocation_drift` — adds threshold inflation per §6.5.
  - Future synthesizer file (per Spec D) — preamble injection per §6.1 (deferred if Spec D not landed).
  - Future state_observer (Spec B) — already covered in Spec B's design; no work here beyond writing the writer (already done in commit #3).
- Tests: each consumer's reliability-read happens (mock the view); anti-feedback-loop contract — downweighted source still has writer-emitted predictions logged.
- Codex zigzag: per-consumer weight-application correctness, anti-feedback-loop contract.

### Commit #7 — Discord 14-day backfill job

- New file `argosy/services/predictions/discord_backfill.py`.
- Registered with Spec A as `predictions-backfill-discord` (manual-trigger-only).
- New route `POST /api/jobs/predictions-backfill-discord/run-now`.
- Tests: idempotency on re-run (no duplicate predictions); unparseable messages still create coverage rows; entry-price snapshot at message timestamp not at backfill-run time.
- Codex zigzag: hindsight-bias contract on backfill specifically (the most likely place to mess it up); idempotency contract.

### Commit #8 (optional) — `/admin/source-reliability` UI

- New page `ui/src/app/admin/source-reliability/page.tsx`.
- New route `GET /api/admin/source-reliability` returning the view data.
- Leaderboard + drill-down + manual-override-panel per §8.
- No tests beyond TS typecheck + basic rendering smoke.

## Appendix A — Predictions DDL

### `predictions` DDL

```sql
CREATE TABLE predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL CHECK (source IN (
    'discord', 'news', 'sec_form_4', 'tipranks', 'sec_13f',
    'capitoltrades', 'internal_per_position_thesis',
    'internal_news_signal_analyst', 'internal_state_observer',
    'internal_monitor_flags', 'manual_user'
  )),
  source_ref TEXT NOT NULL,        -- JSON, source-specific shape
  ticker TEXT NULL,                  -- NULL for multi / macro
  direction TEXT NOT NULL CHECK (direction IN (
    'long', 'short', 'neutral', 'multi'
  )),
  entry_price NUMERIC(12,4) NULL,
  target_price NUMERIC(12,4) NULL,
  stop_price NUMERIC(12,4) NULL,
  timeframe_days INTEGER NULL CHECK (timeframe_days IS NULL OR timeframe_days > 0),
  multi_ticker_json TEXT NULL,       -- JSON: [{ticker,direction,weight}]
  entry_prices_json TEXT NULL,       -- JSON: {ticker: entry_price} for multi
  message_id TEXT NULL,              -- stable per-source dedup key
  raw_text_ref TEXT NULL,            -- e.g. 'news_signals.id:423'
  unparseable_reason TEXT NULL,
  -- Codex IMPORTANT 2 fix: event_at is the real-world prediction time
  -- (Discord msg ts, filing date). created_at is row insertion time
  -- (used only for audit). On live ingest event_at ≈ created_at; on
  -- backfill they diverge by up to 14 days.
  event_at DATETIME NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- Codex BLOCKER 2 fix: evaluation_due_at is computed at write time as
  -- event_at + chosen_window_days (per §3.1 writer-side method selection).
  -- Evaluator's due-query keys off this column, NOT raw timeframe_days.
  evaluation_due_at DATETIME NOT NULL,
  -- Codex BLOCKER 1 fix: evaluation_method is FK-style into the registry
  -- table (NOT a CHECK enum), so new method versions land via INSERT into
  -- evaluation_method_registry, not a schema migration.
  evaluation_method TEXT NOT NULL REFERENCES evaluation_method_registry(method),
  -- Codex IMPORTANT 4 fix: retention column. Set TRUE by the
  -- predictions-retention-compact job after 2 years + 90d-inactive.
  archived BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX ix_predictions_source_event
  ON predictions (source, event_at DESC);
CREATE INDEX ix_predictions_ticker_event
  ON predictions (ticker, event_at DESC)
  WHERE ticker IS NOT NULL;
CREATE UNIQUE INDEX ix_predictions_source_messageid
  ON predictions (source, message_id)
  WHERE message_id IS NOT NULL;
CREATE INDEX ix_predictions_due_at ON predictions (evaluation_due_at)
  WHERE archived = FALSE;
CREATE INDEX ix_predictions_event_at ON predictions (event_at);
```

### `evaluation_method_registry` DDL (Codex BLOCKER 1 fix)

```sql
CREATE TABLE evaluation_method_registry (
  method      TEXT PRIMARY KEY,
  family      TEXT NOT NULL,           -- 'target_stop', 'fixed_lookahead', 'multi_basket', 'unparseable'
  version     INTEGER NOT NULL DEFAULT 1,
  is_active   BOOLEAN NOT NULL DEFAULT TRUE,
  superseded_by TEXT NULL REFERENCES evaluation_method_registry(method),
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  notes       TEXT NULL
);
INSERT INTO evaluation_method_registry (method, family, version) VALUES
  ('target_stop',           'target_stop',     1),
  ('fixed_lookahead_7d',    'fixed_lookahead', 1),
  ('fixed_lookahead_30d',   'fixed_lookahead', 1),
  ('multi_basket_weighted', 'multi_basket',    1),
  ('unparseable',           'unparseable',     1);
```

Adding `fixed_lookahead_30d_v2`: one INSERT (method='fixed_lookahead_30d_v2', family='fixed_lookahead', version=2, is_active=TRUE), plus one UPDATE on `fixed_lookahead_30d` setting `is_active=FALSE, superseded_by='fixed_lookahead_30d_v2'`. No schema migration. The view picks v2 from the next query.

### `prediction_outcomes` DDL

```sql
CREATE TABLE prediction_outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prediction_id INTEGER NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
  outcome_kind TEXT NOT NULL CHECK (outcome_kind IN (
    'hit_target', 'hit_stop', 'expired_neutral',
    'expired_positive', 'expired_negative', 'unparseable'
  )),
  pnl_pct NUMERIC(7,4) NULL,         -- NULL for unparseable
  evaluated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- Codex BLOCKER 1 fix: FK into registry, not a CHECK enum.
  evaluation_method TEXT NOT NULL REFERENCES evaluation_method_registry(method),
  entry_price_used NUMERIC(12,4) NULL,
  exit_price_used NUMERIC(12,4) NULL,
  exit_trigger_date DATE NULL,
  evidence_json TEXT NULL,
  notes TEXT NULL
);
CREATE UNIQUE INDEX ix_outcomes_pred_method
  ON prediction_outcomes (prediction_id, evaluation_method);
CREATE INDEX ix_outcomes_evaluated ON prediction_outcomes (evaluated_at);
CREATE INDEX ix_outcomes_kind ON prediction_outcomes (outcome_kind);
```

`active_evaluation_methods` from the original draft is REMOVED. Its function (selecting which method version is active) is now carried by `evaluation_method_registry.is_active`, which is per-method-version, not a separate config table. The view in §4.1 joins the registry directly.

(Views in §4.1 ship in commit #5.)

## Appendix B — Scoring rule worked examples (codex probe surface)

### Example 1 — Discord call hits target cleanly

- Prediction: source=discord, ticker=NVDA, direction=long, entry=145, target=180, stop=135, timeframe=7d, created_at=2026-05-15.
- Bars 5/16 to 5/22:
  - 5/16: high=148, low=143, close=147 — no hit.
  - 5/17: high=155, low=146, close=153 — no hit.
  - 5/18: high=182, low=148, close=178 — target hit (high≥180), gap-through detection: open=151 (below target), so touched intra-bar → exit at 180.
- Outcome: kind=`hit_target`, exit=180, pnl_pct=+24.1%, exit_trigger_date=2026-05-18.

### Example 2 — Discord call hits stop on overnight gap-down

- Prediction: source=discord, ticker=AMD, direction=long, entry=200, target=230, stop=190, timeframe=7d.
- Day 3: bar = {open: 185, high: 187, low: 184, close: 186}.
- Stop hit (low≤190). Gap-through detection: open (185) is below stop (190) → exit at open=185 (gap hurts).
- Outcome: kind=`hit_stop`, exit=185, pnl_pct=-7.5% (worse than the assumed -5% from the stop).

### Example 3 — Both hit same day (Codex IMPORTANT 1 — adverse-first rule)

- Prediction: long NVDA, entry=145, target=180, stop=140, timeframe=7d.
- Day 4: bar = {open: 144, high: 183, low: 138, close: 142}.
- Both target and stop hit → v1 rule = adverse-first → outcome=`hit_stop`, exit=140, pnl_pct=-3.4%. (Distance doesn't enter the rule any longer; previous draft's "stop closer → stop wins" computation is now obsolete.)

### Example 4 — Internal state_observer flag with no levels

- Prediction: source=internal_state_observer, ticker=NVDA, direction=short, entry=145 (adapter snapshot at flag time), target=NULL, stop=NULL, timeframe=30d.
- evaluation_method = `fixed_lookahead_30d`.
- 30 trading days later: last_close=132.
- raw_return = -9.0%. direction=short → signed_pnl=+9.0%. abs(9%) >= 10%? No.
- 9% > 1% → outcome=`expired_positive`, pnl_pct=+0.090.

### Example 5 — 13F filing (long) with mid-window split

- Prediction: source=sec_13f, ticker=AAPL, direction=long, entry=180 (filing-date snapshot), timeframe=30d.
- Day 15: AAPL does a 4-for-1 split. Adapter returns split-adjusted bars (entry_price was also split-adjusted at writer-time → still 180 in pre-split terms, OR adapter's `get_close_at_or_before` returns split-adjusted bars consistently → entry would be 45 if normalized).
- **Risk:** if entry and exit use different split conventions, the math is wrong. **Mitigation:** evaluator detects split via `adapter.get_split_events(ticker, from, to)` and re-snapshots entry to split-adjusted before scoring. Adds a `notes` field "split-adjusted on YYYY-MM-DD". This is in §3.3 edge cases and ships in commit #4.

### Example 6 — Multi-basket with delisted constituent

- Prediction: source=internal_per_position_thesis, direction=multi, basket=[{"NVDA","short",0.5}, {"DEAD","long",0.5}], timeframe=30d.
- DEAD ticker has zero bars (delisted).
- Score: total surviving weight = 0.5 (NVDA only). NVDA short pnl = (entry - last_close)/entry. Re-normalize: weighted_pnl = nvda_pnl * 0.5 / 0.5 = nvda_pnl.
- Outcome notes = "excluded DEAD: no data".

## Appendix C — Codex review focus

For the codex tandem zigzag review of this spec, focus areas:

1. **Scoring-rule determinism.** §5.1-5.4 + the §5.3 same-day rule. Given (predictions row, bars list), does every run produce the same outcome? Specifically: probe the same-day target+stop rule's asymmetry — is it symmetric for short vs long? Is the "stop closer to entry → stop wins" rule the right asymmetry, or should it be the opposite?

2. **Hindsight bias.** §2.3 + §7.2 backfill. The writer takes a snapshot at `created_at`, NOT at evaluation/backfill time. Probe: are there any code paths where the evaluator could end up reading an entry_price that wasn't pinned at prediction time? Especially: the multi-basket `entry_prices_json` — is the snapshot done correctly per-ticker at write time?

3. **Schema flexibility.** §1.4 — does the single table actually handle free-text Discord + structured 13F + qualitative state-observer flags without source-specific columns? Probe: what would tipranks structure look like? Would it require a new column? If yes, the schema isn't general enough.

4. **Feedback-loop risk.** §6.6 — five mitigations: signals always logged, min-sample floor, clipping, rolling window, manual override. Probe: are any of these mitigations insufficient? Specifically: could a source's reliability spiral to 0.25× and stay there because the 30-day window keeps re-confirming a low hit-rate? Is there an "exit the doghouse" mechanism beyond manual override?

5. **Ledger growth + retention.** §9 estimate (~100k rows/year). Probe: the `evidence_json` column on `prediction_outcomes` could be tens of KB per row for a long-window prediction (30 days of bars). Should the migration include a retention/archival policy in v1? Or is post-1-year archival a follow-on?

6. **Anti-feedback specifically on internal agents.** §2.4 — internal agents are first-class sources. If `internal_per_position_thesis` is scored poorly and downweighted in synthesizer, does it still get to produce predictions for the next cycle? Yes per §6.6.1 (signals always logged). Probe: is there a perverse incentive — the agent could "play it safe" with HOLD verdicts to avoid being scored? Mitigation in §2.4: HOLD is NOT logged as a prediction. So safe-playing reduces the agent's prediction count without changing its hit-rate calc. Probe: is this enough?

7. **Replay semantics.** §3.4 + §5.6 — new evaluation_method = new outcome rows; existing rows immutable. Probe: when active_evaluation_methods config changes, does the source_reliability view recompute correctly? Is there a stale-cache risk if the in-memory reliability cache (5-min TTL) isn't invalidated on config change?

8. **Discord backfill specifically.** §7 — does the backfill flow actually produce comparable reliability vs a hypothetical "live for 14 days" run? Probe: the backfill uses the same news_extractor, the same writer, the same evaluator. So in principle yes. But: the backfill's `adapter.get_close_at_or_before(ticker, msg_timestamp)` requires the adapter to support historical-close-by-date lookups. Does finnhub_adapter expose this? Does yfinance_adapter? If not, the spec needs an adapter extension before commit #7 can ship.

9. **Multi-basket edge cases.** §5.4 — basket with all-delisted constituents → unparseable. Probe: basket with one constituent having huge weight that's delisted → does the re-normalization to surviving weight produce reasonable numbers? Worked example 6 demonstrates the path; double-check the math.

10. **Adapter call volume.** §3.2 — evaluator fetches bars per (ticker, window). Cache is 24h TTL. For ~30 due predictions per day across ~15 unique tickers, that's ~15 adapter calls/day. Within Finnhub free tier (60/min). Probe: a Discord backfill of 14 days running once produces ~50-100 unique tickers — does this exceed the free tier in a single backfill run? Should the backfill chunk/throttle adapter calls?

## Appendix D — Test plan

Per-commit test highlights, focused on the deterministic scoring rule + adversarial edge cases.

### Commit #4 test plan (evaluator + scoring) — the heaviest

`tests/services/predictions/test_scoring.py`:

- `test_target_stop_clean_target_hit` — Example 1 above.
- `test_target_stop_gap_through_stop` — Example 2.
- `test_target_stop_same_day_both_hit_stop_closer` — Example 3.
- `test_target_stop_same_day_both_hit_target_closer` — inverse of Example 3.
- `test_target_stop_short_symmetric` — verify short predictions get the symmetric same-day rule (target closer → target wins on short too).
- `test_fixed_lookahead_classification_thresholds` — verify the +10% / -10% / ±1% boundaries.
- `test_multi_basket_one_delisted` — Example 6.
- `test_multi_basket_all_delisted_unparseable` — basket with no surviving constituents.
- `test_multi_basket_renormalization_math` — weights {0.4, 0.3, 0.3}, one delisted (0.3 weight), surviving weights renormalize to {0.57, 0.43}.
- `test_split_adjustment_detected` — Example 5; bars span a split, evaluator notices + adjusts entry.

`tests/services/predictions/test_evaluator.py`:

- `test_evaluator_idempotent` — run twice, same outcome row count, no duplicates.
- `test_evaluator_skips_not_yet_due` — prediction with timeframe ending tomorrow → not picked up.
- `test_evaluator_skips_already_scored` — prediction with outcome under active method → not re-scored.
- `test_evaluator_replay_inserts_new_method` — switch active method → new outcome row, old row untouched.
- `test_evaluator_delisted_ticker_unparseable` — ticker with empty bars → unparseable + notes.
- `test_evaluator_transient_adapter_error_no_outcome` — adapter raises → no row inserted, evaluator continues to next prediction.
- `test_evaluator_weekend_handling` — created_at on Friday, target hit on Monday → trigger_date=Monday, no off-by-one in weekend gap.

`tests/services/predictions/test_hindsight_bias_canary.py`:

- `test_writer_never_reads_future_price` — mock adapter raises if asked for a date > created_at; writer must not raise.
- `test_evaluator_never_reads_pre_creation_bars` — mock adapter raises if asked for a date < created_at - 1 trading day; evaluator must not raise.
- `test_backfill_entry_price_at_message_time_not_run_time` — backfill a 14-day-old message, snapshot must equal close at message timestamp, not close at backfill-run-time.
- `test_event_at_distinct_from_created_at_on_backfill` — Codex IMPORTANT 2 — `event_at` set to message timestamp (10 days ago), `created_at` set to NOW(); evaluator window keys off `event_at`.
- `test_hold_verdict_logged_as_neutral_prediction` — Codex BLOCKER 3 — per_position_thesis with verdict=HOLD must produce a `predictions` row with `direction='neutral'`.
- `test_evaluation_due_at_30d_for_long_timeframe_source` — Codex BLOCKER 2 — 13F prediction with `timeframe_days=90` must have `evaluation_due_at = event_at + 30d` (not +90d).
- `test_evaluation_method_fk_into_registry` — Codex BLOCKER 1 — insert a prediction referencing a method NOT in the registry → fails. Insert a new method into the registry → predictions can reference it without schema migration.
- `test_anti_double_application_provenance_stamping` — Codex IMPORTANT 3 — feed discord signal through news_signal_analyst (0.5× applied), into synthesizer; synthesizer's `provenance_weights_applied['discord']` is read, NOT re-multiplied; `cumulative_attenuation` stays at 0.5×.
- `test_retention_compact_after_one_year` — Codex IMPORTANT 4 — synthetic 13-month-old outcome row; retention job replaces `evidence_json` with 3-bar compact + writes cold-store archive file; view aggregations unchanged.

### Commit #5 test plan (reliability view + service)

`tests/services/predictions/test_reliability.py`:

- `test_hit_rate_basic` — 10 predictions, 6 hit_target, 4 hit_stop → hit_rate=0.6.
- `test_unparseable_excluded_from_hit_rate_included_in_coverage` — 10 predictions, 6 hit/4 unparseable → hit_rate=1.0 over 6 scored, coverage_pct=0.6.
- `test_rolling_window_30d_vs_90d` — 100 predictions over 90d, last 30d hit-rate differs from full-window → both report correctly.
- `test_effective_weight_min_sample_floor` — source with 5 outcomes, all hits → effective_weight=1.0 (not 2.0).
- `test_effective_weight_clipping` — source with 20 outcomes, all hits → effective_weight=4.0 (clipped, not 2.0); source with 20 outcomes, all misses → effective_weight=0.25 (clipped).
- `test_manual_weight_override` — set manual weight 1.5×, expires_at=tomorrow → reliability returns 1.5×; expire date passes → reverts to computed.
- `test_short_direction_pnl_signed` — short prediction with last_close < entry → pnl_pct positive (short profits).

### Commit #6 test plan (consumer integration)

`tests/services/predictions/test_consumer_integration.py`:

- `test_news_signal_analyst_reads_reliability_per_source` — mock reliability for discord at 0.5×, news at 1.2×; agent prompt contains both factors.
- `test_per_position_thesis_blends_news_signal_reliability` — verified via the `reasoning_md` field of the resulting `PositionThesis`.
- `test_anti_feedback_writer_logs_regardless_of_consumer_weight` — set discord weight to 0.25×, send 10 discord messages; all 10 still create predictions rows.
- `test_plan_monitor_drift_threshold_inflates_on_low_reliability` — mock `internal_monitor_flags` reliability at 0.3 hit-rate → persistent_threshold inflates from 0.10 to 0.13.

### Commit #7 test plan (Discord backfill)

`tests/services/predictions/test_discord_backfill.py`:

- `test_backfill_dedup_on_rerun` — 50 messages, run twice → 50 predictions rows, not 100.
- `test_backfill_creates_unparseable_for_no_ticker_messages` — chat messages without tickers → predictions row with unparseable_reason set.
- `test_backfill_entry_price_at_message_timestamp` — message from 10 days ago, entry_price = adapter close on that date.
- `test_backfill_missing_creds_returns_422` — no creds file → API route returns 422; no rows inserted.
- `test_backfill_triggers_evaluator_after_completion` — backfill 14d, evaluator should immediately process all (since they're all past-timeframe by definition).

## Section 11 — Codex review integration (2026-05-29)

**Initial verdict:** BLOCK (codex single-dispatch reviewer, gpt-5-5, 23,674 tokens, 39.2s wall, session `tools/codex-tandem/sessions/2026-05-29-predictions-ledger-spec-review/research/single_review_node_01/`). After integrating all BLOCKERs + IMPORTANTs below: **APPROVE_WITH_CONDITIONS** (conditions = the v1 retention job in §9.1 must actually ship in commit #4; the `evaluation_method_registry` migration lands in commit #2; tests verifying anti-double-application stamping land in commit #6).

### BLOCKERs (3) — all integrated

| # | Finding | Where (codex) | Integrated where |
|---|---|---|---|
| 1 | Replay design could double-count predictions when multiple methods active; original CHECK enum on `evaluation_method` contradicted the "add new method values" replay strategy | §3.4, §4.1, §5.6, Appendix A | §1.2 (column desc), §3.4 (replay rewrite + method registry), §4.1 (view SQL rewrite picks ONE outcome per prediction per family), Appendix A (DDL — FK into `evaluation_method_registry`, removed `active_evaluation_methods`) |
| 2 | Due-selection logic used raw `timeframe_days` so a 13F prediction with timeframe=90 couldn't be scored at 30d (per §5.5) until 90d had passed | §3.1 vs §5.5 | §1.2 (`evaluation_due_at` + `evaluation_method` columns), §2.3 (event_at vs created_at), §3.1 (rewritten due query keys off `evaluation_due_at`), Appendix A DDL |
| 3 | HOLD exclusion created selection-bias / gameable incentive — agent could hide behind HOLD and inflate reliability | §2.4, §6.6 | §2.4 (HOLD verdicts ARE logged as `direction='neutral'`, scored via `fixed_lookahead_30d` against price action; added `abstain_rate` to reliability view + optional `participation_penalty` consumer factor) |

### IMPORTANTs (4) — all integrated

| # | Finding | Where (codex) | Integrated where |
|---|---|---|---|
| 1 | Same-day target/stop "distance-conditional" rule was arbitrary, biased against wide-target setups, and broke cross-source comparability | §5.3 | §5.3 (rewrote to "always adverse-first" — distance-invariant, symmetric, deterministic), §5.1 (code update), §3.3 edge-case row, Appendix B Example 3 |
| 2 | Backfill timestamp semantics under-specified; `created_at` defaulting to insert time risked drift on backfill | §2.3, §7.2 | §1.2 (`event_at` column, NOT NULL), §2.3 (rewritten — `event_at` distinct from `created_at`; writers MUST pass it; evaluator anchors at event_at), §3.1 (window = event_at ± window_days), Appendix A DDL |
| 3 | Stacked consumers could double-apply reliability (discord → news_signal_analyst → synthesizer compounds the discount) | §6.1-§6.6 | §6.6 (new mitigation 6 — `provenance_weights_applied` stamping + `cumulative_attenuation` floor of 0.10 + test contract for commit #6) |
| 4 | Retention policy stated but not operationalized — no job, no migration, no enforcement | §9 | §9.1 (new subsection — `predictions-retention-compact` weekly job, 3-bar compact summary at 1y, archive cold-store at 2y inactive, ships in commit #4, tests in commit #4) |

### NICE (1) — acknowledged, not integrated

| # | Finding | Where | Disposition |
|---|---|---|---|
| 1 | Non-price portfolio/meta predictions (e.g. `mc_regression`) stretch the ticker-price row model | §1.4, §2.4 | Acknowledged. The single-table flexibility is intentional (per the spec's central design claim); typed target objects can layer on as JSON inside `source_ref` if needed. Revisit if the meta-prediction count exceeds ~10% of total. No v1 change. |

### Confirmed-no-design-change items

- **Schema flexibility across source shapes** (§1.4) — codex did not flag this as a BLOCKER. The single-predictions-table claim survives review with the NICE caveat above.
- **Hindsight-bias contract on the multi-basket `entry_prices_json`** (§5.4) — codex did not flag.
- **Internal-agent first-class scoring as a category** — codex's BLOCKER 3 surfaced the HOLD edge case but did NOT object to the overall design. The user's binding preference stands.
- **Min-sample floor (20) + clipping range [0.25, 4.0] + rolling 30-day window** — codex did not flag the parameter values, only the double-application risk (now fixed via IMPORTANT 3).

### Open items deferred to follow-on

- The "exit the doghouse" mechanism beyond manual override + 30d window — codex implicitly questioned this in IMPORTANT 3 framing but no explicit finding. Current design (the 30d rolling window naturally allows recovery within a month + manual override as escape hatch) is judged sufficient for v1. Revisit if a real source spirals to 0.25× and stays there.
- Provenance-aware weighting tests in commit #6 must exercise multi-hop attenuation (discord → news_signal_analyst → synthesizer) to lock the contract. Listed in the §10 commit #6 test plan.

## Section 12 — Open dependencies for Ariel

Three items needed mid-sprint, none blocks the start:

1. **Discord backfill activation** (blocks commit #7 execution, not commit drafting): assumes `~/.argosy/discord_creds.json` from Spec #1 already on disk + Spec #1 commit #16's listener has read-rights on the channel. If not done, commit #7 ships dormant + writes a stub manual-run page that explains how to enable.
2. **Spec A's JobRegistry shipped before commit #4**: this spec's evaluator + backfill register with Spec A. If Spec A is still in design when this spec ships commit #4, fallback registration is via cron entry in `argosy/scripts/cron.sh`. Spec D may finalize JobRegistry shape, in which case migrate.
3. **Spec B's state_observer shipped before commit #3's stub becomes a real writer**: commit #3 ships a `write_state_observer_prediction` function that's a no-op-on-empty-input until Spec B's observer emits flags. Once Spec B lands, no code change needed here — the writer's `if not flag: return None` short-circuits until producer signals arrive.
