# Early-Signal Streams — design for the discovery pipeline

**Status:** shared contract, calibration, transparency, and Stream A are implemented;
Streams B, D, C, and E remain specified for the build order in §6.
**Audience:** an implementing agent with no prior session context. Read `docs/design/SDD.md`
§"Quickstart for new agents" first, then this file. The reviewing agent is whoever holds the
current session (review = code review + verify-run of the first live cycle per stream).

## 1. Problem

The discovery funnel (radar → estimator → fleet adjudication) finds candidates for the plan's
high-growth sleeve (two lanes: strict asymmetric "moonshot" + "market-beating alpha" — the lane
mandates live IN the current plan's high-growth class and are read via
`argosy/services/decision_funnel/sleeve_mandate.py`; never hardcode them). Today the radar's only
input families are price/volume MOMENTUM, social ATTENTION, and fundamentals GROWTH
(`argosy/services/trend_radar.py::_FAMILY_SCORE`). These are *concurrent-or-late* signals: by the
time they fire, the move is often underway. The goal is EARLY signals — public data that predates
price recognition (the canonical example class: government-contract ramps visible in public award
data quarters before the market re-rates the stock).

**Non-negotiable doctrine** (memory-backed, verbatim intent):
- A signal NEVER buys anything. Signals only nominate names into the existing funnel where the
  fleet adjudicates against the plan-owned lane mandates. (Fleet authors; determinism verifies.)
- Every stream is SCORED before it is WEIGHTED: each nomination writes a prediction to the
  ledger (`predictions` table) and the outcome evaluator scores it; a stream earns funnel weight
  only from its own ledger record. (Unified source scoring; the alpha-report backtest is the
  template — see §5.)
- No per-symptom detectors; each stream is one adapter behind ONE shared contract (§3).

## 2. Full catalog of signal families (documented options)

Selected-for-build streams are marked ▶ (§4). Evidence grades are honest, not promotional.

| # | Family | What it catches | Evidence | Lag | Verdict |
|---|---|---|---|---|---|
| 1 | ▶ Government contract awards (USAspending, SBIR, DoD/DOE/NRC) | Revenue visible in public records before earnings | Strong (primary data, not opinion) | Days | **Build — stream A** |
| 2 | ▶ Insider cluster buys (SEC Form 4) | Multiple insiders open-market buying, esp. small caps | Strong (academic support for clusters; single buys weak) | 2 days (filing deadline) | **Build — stream B** |
| 3 | ▶ Known-names smart money: congressional PTRs + superinvestor 13Fs | Conviction entries by tracked individuals (politicians, famous fund managers) | Mixed/weak for politicians (see `domain_knowledge/data_sources/capitoltrades.md` — cluster-driven, methodology-sensitive); moderate for concentrated-fund 13F NEW positions | 30-45d (PTR), up to 135d (13F) | **Build — stream C, sentiment-tier by default until its ledger says otherwise** |
| 4 | ▶ Earnings acceleration + estimate-revision breadth | Accelerating revenue + first margin inflection; analyst revisions chase late (PEAD) | Strong (one of the most robust anomalies) | Quarterly + revision drip | **Build — stream D** |
| 5 | ▶ Hiring & product traction (job postings velocity, app-store rank velocity, GitHub traction for infra names) | Operational reality ahead of reported numbers | Moderate; less crowded | Weeks-months ahead | **Build — stream E** |
| 6 | Attention velocity (Reddit/X mention growth before price) | Narrative formation | Weak alone; useful as a deep-review trigger | Concurrent-ish | Exists (radar ATTENTION family); improve later |
| 7 | Index-inclusion + IPO lockup mechanics | Forced-buyer flows | Moderate, short-lived | Scheduled | Defer (execution-tactical, not discovery) |
| 8 | Pundit/channel feeds (scored per source) | Leads + scored bear warnings | Per-source ledger only (one scored source shows edge concentrated in SHORT calls) | Daily | Exists (alpha-report path); generalize via §3 contract later |
| 9 | Patent filings / FDA-calendar biotech catalysts | Deep-tech and biotech milestone runways | Weak-moderate, high noise, domain-specific parsing | Months | Defer |
| 10 | Options flow (unusual activity) | Positioned conviction | Weak-mixed, data is paid, heavily crowded | Concurrent | Defer |

**Answer to "does tracking known names (e.g. a famous politician) fall under smart money?"** —
Yes: stream C. Congressional PTRs and superinvestor 13Fs are the same family: *identified
individuals with plausible information or skill advantages, publicly disclosed with a lag.*
The evidence for politician-trade alpha is weak and cluster-driven; the design therefore admits
the stream but caps it at sentiment-tier weight until its OWN ledger score proves more. Named
tracking is a config list, not code (see stream C).

## 3. Shared architecture — the SignalStream contract

One new module family: `argosy/services/signal_streams/` with a common adapter interface.
This is the only new abstraction; everything downstream already exists.

```
argosy/services/signal_streams/
    base.py          # SignalStream protocol + SignalNomination dataclass
    contracts.py     # stream A
    insider.py       # stream B
    known_names.py   # stream C
    earnings_accel.py# stream D
    traction.py      # stream E
```

```python
@dataclass(frozen=True)
class SignalNomination:
    ticker: str
    stream: str              # 'gov_contracts' | 'insider_cluster' | 'known_names' | ...
    direction: str           # 'long' | 'short'  (streams may emit shorts, e.g. insider cluster SELLS)
    strength: float          # 0..1, stream-normalized
    as_of: date              # the EVENT date (award date / filing date), not fetch date
    evidence: dict           # raw fields + source URLs (auditable to raw rows)
    dedup_key: str           # stable per underlying event; idempotent re-runs
```

Each stream implements `fetch(session, *, since: date) -> list[SignalNomination]`. A single new
scheduler loop `signal_streams_daily` (cron, follow `discovery_funnel_loop.py`'s cron pattern —
interval schedules re-anchor on restart and starve; that bug is documented) runs all streams with
per-stream failure isolation (one failing stream never blanks the rest — mirror the inbox
adapter-isolation pattern in `argosy/services/inbox/service.py::build_inbox`).

**Downstream wiring (existing machinery, do not rebuild):**
1. Nominations upsert into the radar candidate store (`trend_scan_state` via
   `argosy/services/high_potential_funnel.py`) with the stream recorded in the fingerprint —
   they flow through the SAME estimator → fleet path as radar names. Radar liquidity gates
   (price/cap/volume bands in `trend_radar.py`) still apply. Cap band note: the radar cap ceiling
   and the lane cap ceilings are plan/policy-owned; read them, don't restate them.
2. EVERY nomination also writes a ledger prediction (`predictions` table,
   `source='signal_stream:<name>'`, entry price snapshotted AT WRITE TIME — this was the gap that
   silently killed the first backtest: the alpha-report writer stored NULL entries and the
   evaluator refused 318 rows for weeks. The v2 entry-backfill evaluator (migration 0081,
   `fixed_lookahead_*_entry_backfilled`) exists as a safety net; do not rely on it — snapshot at
   write). Timeframes: emit BOTH 30d and 180d predictions per nomination (two rows) so tactical
   and thesis horizons score separately.
3. The outcome evaluator scores them on schedule (`predictions_evaluator` loop). ALSO WIRE (small,
   in scope): the re-evaluation batch (`run_reevaluation_batch`) into the evaluator loop so
   recovered/late rows self-heal — it is currently on-demand only.
4. **Weighting:** the funnel's stage-2 triage prompt receives each nomination's stream name +
   that stream's current ledger scorecard (win rate, n, avg pnl) as CONTEXT. No deterministic
   weight multiplication — the fleet judges with the score in view (determinism supplies facts).
   A stream with n<30 scored outcomes is labeled "uncalibrated (beta — N scored over M days)"
   per the nothing-hidden doctrine.

## 4. The five streams

### A. Government contract awards (`gov_contracts`) — build FIRST
- **Sources:** USAspending.gov API (`/api/v2/search/spending_by_award/` — free, no key, POST JSON
  filters; docs at api.usaspending.gov); SBIR.gov awards API; optionally SAM.gov (needs free key).
- **Logic:** daily fetch of new prime awards; map recipient → public ticker (build a
  recipient-name→ticker map: seed from a static list of public gov-contractors + fuzzy match +
  LLM-assisted resolution for unknowns, persisted so each name resolves once). Nominate when a
  ticker's trailing-90d award total is a material fraction of its trailing-12m revenue
  (materiality threshold as % of revenue, NOT absolute $; revenue from the existing fundamentals
  adapter). strength = award$/revenue ratio, capped.
- **Sharp edge:** award ≠ revenue timing (IDIQ ceilings vs obligations — use obligated amounts,
  not ceilings). Exclude mega-caps by the radar band anyway.
- **Effort:** ~2-3 sessions incl. the name-resolution map. **Acceptance:** replays a known
  historical case (a defense/AI software name whose award ramp preceded re-rating) from fixture
  data; live cycle produces ≥1 nomination with full evidence URLs; ledger rows written with entry
  prices.

### B. Insider cluster buys (`insider_cluster`)
- **Sources:** SEC EDGAR Form 4 full-text feed (free, `https://www.sec.gov/cgi-bin/browse-edgar`
  ATOM or the daily index files; respect the 10 req/s SEC fair-access rule with a UA string).
  A Form-4 parser was previously queued ("13F/Form4 smart-money family post-parser-fix") — check
  `domain_knowledge/data_sources/sec_form4.md` for the recorded parser gotchas before writing one.
- **Logic:** nominate LONG on a cluster = ≥2 distinct non-10b5-1 open-market BUYS (code P, not
  option exercises) by officers/directors within 14 days, aggregate ≥ configurable $ floor scaled
  by market cap. Nominate SHORT-tier *warning* (not a funnel candidate — a monitor flag) on
  cluster sells only if C-suite + >20% of holder's stake (sells are noisy: diversification/taxes).
- **Effort:** ~2 sessions. **Acceptance:** parser unit-tested on fixture filings incl. amended
  filings + 10b5-1 exclusion; a replayed historical cluster nominates; dedup idempotent across
  re-fetches.

### C. Known-names smart money (`known_names`)
- **Sources:** House/Senate PTR originals (tier-1: `disclosures-clerk.house.gov`,
  `efdsearch.senate.gov`) with capitoltrades.com as a convenience aggregator (tier-2; see
  `domain_knowledge/data_sources/capitoltrades.md` caveats — coarse amount brackets, 30-45d lag);
  13F quarterly holdings for a configured list of concentrated managers
  (`domain_knowledge/data_sources/sec_13f.md`).
- **Config, not code:** `tracked_names` list (politicians + funds) in agent settings, each entry
  carrying its own ledger sub-source (`signal_stream:known_names:<slug>`) so INDIVIDUALS get
  scored separately — "does following person X work" becomes a queryable fact. Amount-bracket
  floor (e.g. ignore <$15K brackets) configurable.
- **Logic:** nominate LONG on a tracked name's new purchase in a radar-band ticker; 13F NEW
  positions (not adds) in concentrated portfolios nominate at higher strength than PTRs.
- **Honesty rail:** stream starts at sentiment-tier (uncalibrated label) and its per-person ledger
  decides everything thereafter. The 45-day lag is disclosed in the evidence dict.
- **Effort:** ~2 sessions (PTR parsing is the ugly part; start from the aggregator, verify
  against tier-1 originals for a sample). **Acceptance:** per-person sub-source rows in the
  ledger; a fixture-replayed disclosure nominates; lag recorded as (event_date, disclosed_date).

### D. Earnings acceleration + revisions (`earnings_accel`)
- **Sources:** existing fundamentals adapter (quarterly history) + estimate revisions from the
  existing market-data providers (yfinance analyst fields as the free floor; the finnhub adapter
  where it already has entitlements).
- **Logic:** screen the radar-band universe quarterly + on-earnings: nominate when (a) revenue
  growth accelerates two consecutive quarters, AND (b) gross/operating margin inflects positive
  or crosses zero, OR (c) estimate-revision breadth (up-revisions minus down over 60d) exceeds a
  threshold. strength = composite z-score within the screened universe.
- **Effort:** ~1-2 sessions (data already flows; this is mostly a screen). **Acceptance:** the
  screen replays a known historical accelerator from fixtures; thresholds config-owned; no
  look-ahead (only data available as of the screen date — use report dates, not period dates).

### E. Hiring & product traction (`traction`)
- **Sources (free tiers, per-ticker on the radar/watch universe only — this stream ENRICHES, it
  does not scan the world):** job-postings velocity via Greenhouse/Lever public boards or the
  hiring page sitemap deltas; GitHub stars/contributors velocity for developer-infra names;
  app-store rank via an RSS/scrape adapter where a name is consumer-app-driven.
- **Logic:** compute 90d velocity vs trailing baseline for names ALREADY in `trend_scan_state`
  (quarantined included — a traction spike is exactly what should resurrect a quarantined name);
  nominate on >2σ acceleration. This stream deliberately runs LAST in build order: it multiplies
  the others rather than standing alone.
- **Effort:** ~2 sessions. **Acceptance:** enrichment visible on scan-state rows; a synthetic
  velocity spike resurrects a quarantined fixture name into estimation.

## 5. Scoring & calibration plan (all streams)

- Ledger source naming: `signal_stream:<stream>` (+ `:<person-slug>` for stream C).
- Two predictions per nomination (30d, 180d), entry snapshotted at write; direction from the
  nomination. Evaluator: existing v1 + v2 (entry-backfilled) methods; wire `run_reevaluation_batch`
  into the `predictions_evaluator` loop (one flag; the batch exists).
- Per-stream scorecard endpoint/query is the SAME one used for any source (predictions ⨝
  prediction_outcomes grouped by source) — no new reporting machinery; surface the scorecard in
  the funnel transparency card as "signal sources (beta — n scored)".
- Kill rule (recorded here so it's not re-litigated): a stream whose 180d slice shows n≥50 scored
  and win-rate ≤ the always-long-same-tickers benchmark loses funnel-context privileges (its
  nominations still write ledger rows — data keeps accumulating — but stage-2 stops seeing them).

## 6. Implementation order & review protocol

Build order: **A → B → D → C → E** (primary-data strength first; C after B because they share
EDGAR plumbing; E last as a multiplier). One stream per PR-sized block: adapter + tests + loop
registration + one live cycle + its ledger rows verified, then the reviewing agent runs the
verify-run skill on that cycle before the next stream starts. Never wire a new stream and widen
its weight in the same block.

Conventions that WILL bite otherwise (all previously hit in this repo): PowerShell `;` not `&&`;
PYTHONIOENCODING=utf-8 + durable-side-effects-before-prints (cp1252 console); cron not interval
for loops; SEC fair-access UA; new tables via additive Alembic migration following the newest
`alembic/versions/` file's style; tests marked so `-m "not llm_eval"` stays green without keys.
