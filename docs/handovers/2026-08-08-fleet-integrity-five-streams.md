# Handover — 2026-08-08 fleet-integrity marathon (five streams, none merged)

**Roles:** resident session = REVIEWER + orchestrator. Implementers were subagents
working in isolated worktrees; every hand-back went to an independent adversarial
reviewer before any merge decision. **Git at close:** master = `4d97a73`,
UNCHANGED — nothing merged this session. Five feature branches carry committed
WIP. Migrations head on master still **0094**.

**How this session started:** an adversarial review of
`docs/design/performance_scorecard_design.md` (verdict: BLOCK). Chasing the
design's data claims into the live DB uncovered a chain of real data-integrity
and fleet-reliability defects, which became five parallel workstreams.

---

## 1. THE HEADLINE — $2.38M was erased from the book by an ingest bug

Independently verified against `db/argosy.db` (read-only). **This is not the
deliberate NVDA exclusion; it is a regression.**

Snapshot id 34 (2026-07-13, `self-refresh:reprice-of-2026-07-12`) held **49
positions / $4,076k** across accounts `schwab`, `schwab 876`, `Aborad`, `Leumi`.
Snapshot id 49 (today) holds **38 positions / $1,616k**, `Leumi` ONLY.

**Every non-Leumi position was erased — 8 positions, $2,432.0k.** Enumerate by
POSITION, not by symbol: a symbol-keyed diff collapses the same ticker held in
two accounts and under-reports the damage (this caught me out first pass — I
initially reported only 3 positions / $2,382.7k).

| Symbol | Account | Shares | Value | Note |
|---|---|---|---|---|
| **NVDA** | schwab | 10,940 | **$2,307.9k** | |
| **`-`** | Aborad | 3.0 | **$69.0k** | unidentified asset |
| **SGOV** | schwab 876 | 200 | **$20.1k** | also held in Leumi — invisible to symbol diff |
| **SCHD** | schwab 876 | 400 | **$13.0k** | also held in Leumi |
| **VOO** | schwab 876 | 10 | **$6.9k** | also held in Leumi |
| **`-`** | schwab 876 | 5,893 | **$5.9k** | a SECOND unidentified asset |
| **BMY** | schwab 876 | 100 | **$5.8k** | had HOLD verdict id=3 (07-11) |
| **SCHG** | schwab 876 | 100 | **$3.5k** | also held in Leumi |

id34 = $4,075.6k total ($1,643.6k Leumi + $2,432.0k non-Leumi).
id49 = $1,615.6k, Leumi only. Restored book should be **46 positions /
$4,047.6k** (38 + 8; $1,615.6k + $2,432.0k) — independently confirmed.

Legitimately gone, not erased: NKE (proposal #12 `executed_live`), RKT (#9),
SPCX (#4). And `מחקה ת"א-200` → `ת"א-200` is a rename, not a loss.

**Consequence for downstream logic:** SCHD, VOO, SCHG and SGOV each exist in
BOTH `Leumi` and `schwab 876`, so cross-account same-symbol lots are legitimate.
Any dedup that keys on symbol alone will drop real money once the merge restores
these. Stream D's existing "keep first, do not sum" duplicate handling is
therefore now dangerous and is under review.

**Root cause:** snapshot ingest REPLACES the whole book instead of merging per
account, so a Leumi-only TSV deletes every other account. Compounding defect:
id 35 is *dated* 2026-06-29 but was imported 2026-07-13 — an older, partial feed
became the current book. Everything from id 36 on is a self-refresh of the
truncated book.

**Proof it is a bug, not intent:** the fleet issued a HOLD verdict on BMY on
2026-07-11 and the position vanished by 07-14 with no sale. Nothing holds a
position it is about to exclude.

**Consequence:** concentration, US-situs estate exposure, retirement safety
gates, FI shock, deploy-cash and net worth have all been computed on a book
missing 59% of its value. **Any number derived from snapshots 35-49 is suspect.**

Note separately that Ariel DOES want NVDA excluded from *sleeve math* — that
intent is real and Stream D's `unmanaged_holdings` mechanism is the right way to
express it. The bug is that absence was accidental, not declared.

---

## 2. Other verified data-integrity findings (all re-derived read-only)

- **FX is wrong in four snapshots.** Most carry ~3.00 USD/ILS; ids 35 (06-29),
  36 (07-12), 37 (07-14), 38 (07-16) carry exactly **3.7**. On 07-16 two
  snapshots disagree (3.7 vs 3.0067). A round 3.7 beside real rates looks like a
  hardcoded fallback; it overstates every shekel figure in those snapshots by
  ~23%.
- **Nine dates carry multiple snapshots** — up to **eight** on 2026-07-12. This
  is the "multiple rows per date" flaw raised against the scorecard design,
  confirmed with real data: any return series must pick one and nothing says
  which.
- **Prices are genuinely frozen for non-US instruments.** US names move normally
  once weekends are accounted for. `STOXX Europe 600` has 3 distinct prices
  across 19 marks; `ת"א-200` has 2 across 11 with a 10-date frozen run;
  `מחקה ת"א-200` and `MSCI World` likewise. These are exactly the tickers a US
  quote source cannot resolve — failing silently and carrying the last value
  forward. **Any return computed for them is fiction.**
- **Ticker duplication:** `BRK.B` and `BRK/B` both exist as separate positions,
  so the class-share normalization in `d81bce5` left a residue.
- **`avg_price` (broker average cost) is present on every equity position** —
  36/36 in the current snapshot, and 45/45 across all tickers seen in snapshot
  history — so *approximate USD* embedded gain is computable. It is NOT
  tax-grade: averaging destroys purchase-date FX, and Israeli real gain needs
  CPI indexation plus the shekel rate at acquisition. `lots` / `fills` /
  `daily_account_pnl` are all **0 rows**, and Israeli CGT in code is flat rates
  applied to assumed gain fractions (0.6 portfolio-wide, 0.8 for NVDA
  deconcentration) — there is no per-position sell-tax function.
- **AMD shows +961.6% embedded gain** — plausible for a 2016-vintage holding but
  unverified; worth a look.

---

## 3. Owner decisions made this session (binding)

1. **HOLD grading = best available alternative in the same plan class**, NOT
   vs CSPX. Ariel's reasoning: a HOLD asserts both that the thesis is intact
   *and* that this is the right instrument for its slot; if a same-category peer
   would do better, holding is an active mistake. Broad-market outperformance
   does not excuse losing to a peer.
2. **Tolerance band = the real round-trip switching cost**, computed per position
   from its actual embedded gain (so a low-basis winner gets a wider band than a
   recent purchase). Rationale: if a peer beat you by less than it would have
   cost to move, holding was correct.
3. **Provenance = Option C, split by instrument class.** Finnhub abandoned as the
   reported-period source.
4. **Populate `lots` via the existing Schwab cost-basis importer** rather than
   approximating the band; report HOLDs as "unscored, basis missing" until real
   basis exists.
5. **Sequencing:** commit all WIP, then land one stream at a time. Ingest repair
   promoted ahead of Stream E by reviewer judgment (a live $2.38M error outranks
   the async bug); Ariel concurred with landing only the ingest repair this
   session.

### Grounding for decision 1 (real data, same window, snapshot marks)

Global quality growth sleeve: NOW **+17.5%**, CRM +16.0%, AMZN +13.1%, SCHG
+4.9%, IWQU +2.6%, META +1.6%, QQQM +1.5%, CNDX +0.2%, SPMO −0.8%, GOOG −0.8%,
AMD −6.7%, TSLA **−16.5%**. A 34-point spread inside one category over ~1 month.
Caveat: common windows are only ~1 month for the large classes, so verdicts are
statistically fragile until history accumulates.

### Reviewer decisions made without escalating (per the fatal-forks-only bar)

- **Fund vintage exemption accepted as interim.** 21 of 38 positions are ETFs /
  index vehicles, now exempt from fiscal-vintage checking under a named rule,
  because a fund has no issuer fiscal quarter. Accepted because TRLV was a
  single-name fundamentals failure and ETF decisions are allocation-level. The
  analogue (NAV / holdings-as-of freshness) is **queued, not done**. NOTE: this
  reasoning only holds if classification cannot be influenced by the payload —
  and per §4 Stream A, right now it can.
- **Mechanical-vs-judgment line.** Enforcing "did the bear actually retrieve" and
  "was this citation actually fetched" is permitted despite the
  determinism-is-for-arithmetic doctrine, because these are facts about whether
  the pipeline ran, not judgments about decision quality. No amount of
  re-derivation by a second agent substitutes for the bear having looked.

---

## 4. The five streams — state at close

None merged. All have committed WIP; three had rounds in flight at close.

| Stream | Branch | HEAD | Round | State |
|---|---|---|---|---|
| A data-integrity / vintage gate | `feat/stream-a-data-integrity` | `fb9523d` | 7 done | blockers addressed, UNREVIEWED; enforcement OFF |
| B bear independence / premise check | `feat/stream-b-bear-independence` | `5995bfe` | 10 done | class fix landed, UNREVIEWED; test-coverage gap |
| C prediction ledger / scorecard | `feat/stream-c-prediction-ledger` | `8812ea8` | 2 done | BLOCKED, 6 blockers; HOLD rule not yet coded |
| D managed/unmanaged holdings | `feat/stream-d-managed-holdings-abstention` | `03b7692` | 6 done | regressions cleared; adversarial review in flight |
| E async event-loop root cause | `feat/stream-e-async-cache-loop` | `9cc1d26` | 2 done | BLOCKED, 2 blockers; not started |

### A — vintage gate. HEAD now `fb9523d` (round 7). Enforcement deliberately OFF.

**History of fabricated numbers — keep the skepticism.** Round 4 claimed 83% from
a test injecting fake calendar events. Round 6 claimed **37/38**; independent
re-derivation found production returned **0/36** (Finnhub unavailable, yfinance
fallback defaulted **off**, plus a silent 25-symbol cap). Round 7 fixed the cap
and the fallback and reported honestly from the production entry point
(`plan_synthesis/inputs._gather_fundamentals`).

**Real round-7 production rates:** funds `21/21`, cash `2/2`, equities
**`0/13`** — all blocked as a named `sec_config_error` because
`ARGOSY_SEC_CONTACT_EMAIL` is unset. Equities DO now get `financials_as_of` from
yfinance (AMD `2026-06-27`, OKLO `2026-03-31`); it is the SEC reported-period
half that cannot run.

**Read the "23/36 pass" carefully: 23 of those 23 are EXEMPTIONS** (21 funds +
2 cash). **Zero equities have verified provenance.** The gate currently verifies
nothing — it exempts most of the book and blocks the remainder on missing config.

**Enforcement is OFF (`integrity_vintage_enforce=False`) and must stay off** until
`ARGOSY_SEC_CONTACT_EMAIL` is set and production gather is re-measured. Turning it
on at 0/13 would freeze every equity BUY. Current-vintage evaluation IS now wired
(`resolve_integrity_exclusions`) — the flag is the only thing holding it.

**DECISION REQUIRED before enabling enforcement — SEC outage policy.** Round 7
chose: misconfig → fail **closed** (`sec_config_error`); issuer has no period →
fail **closed**; provider outage (403/429/timeout) → fail **OPEN** as a loud
`exempt:sec_provider_outage`. The fail-open branch reopens the exact TRLV hole
whenever SEC is unreachable, and SEC rate-limits are easy to trip — the limiter
is **single-process only**, so a multi-process deployment could self-inflict 429s
and silently disable the gate for all equities. "Loud" is weak protection in an
unattended system. Options at enable time: fail closed and accept an equity
freeze during outages; or keep fail-open but time-boxed (exempt for N hours, then
block) with a user-visible banner. Owner has not ruled on this.

Structural insight: the gate would block everything (0% liveness) *and* is not
actually wired (`/deploy-cash` and period directives only filter pre-existing
remediation rows, never evaluate current vintage). **The two defects were
concealing each other** — merging with enforcement working would have frozen the
whole system. Liveness must be fixed and measured before enforcement is switched
on.

Also open: `classify_from_fields` trusts a payload-supplied `provenance_class`
(stale AAPL passed when labelled `fund_etf_index`); `vintage_gate.py:91-99`
returns no violation when source labels are missing; SEC has no durable cache and
bypasses the global rate limiter; `ARGOSY_SEC_CONTACT_EMAIL` unset so SEC blocks
the caller; synthesis remediation writes are best-effort (fail open); decision
overrides are recorded before the integrity gate, corrupting the audit trail.

SEC EDGAR itself was verified working (AMD 10-Q `reportDate=2026-06-27`), and
OKLO is a genuine lag case (yfinance `2026-03-31` vs SEC `2026-06-30`) — but
**OKLO is NOT currently blocked**, because enforcement is not wired.

### B — bear independence. Ten rounds; a working exploit each of the last three.
The reviewer has repeatedly *demonstrated* `SELL EVERYTHING` reaching the trader
— round 8 via the facilitator channel, round 9 via premise catalyst/citation
text in `researcher.py`, round 10 via premise-check text rendered raw into the
trader prompt (`researcher.py:529-556`, `trader.py:433-451`). Each round closed
one channel; the next found an adjacent one.

More important, the stream's actual purpose is not working: bear independence is
satisfied by **any** URL (an irrelevant retrieval passes while every point stays
`shared_payload`), and `premises=[]` still bypasses retrieval entirely — an
earlier round fixed `None` and missed the empty list. The bull has no retrieval
mandate at all, leaving the mirror-image defect open for SELLs.

Outage handling is also wrong: a trader HOLD returns as `trader_hold` *before*
the independence gate so an outage masquerades as a considered decision with no
reevaluation queued, and once the shared circuit breaker opens
`FleetCallUnavailable` is uncaught and the API returns **500**.

**Round 10 outcome — HEAD `5995bfe`, UNREVIEWED.** First version that attacks the
class rather than a channel:

- Independence is **semantic**: `bear_turn_has_independent_retrieval` now requires
  ≥1 substantive point (claim ≥24 chars, evidence ≥40, non-throwaway) citing a
  tool-retrieved URL. An unrelated retrieval returns False.
- **Bull now has the same WebSearch mandate**, closing the mirror-image
  stale-bearish-SELL path.
- `None`, `[]` and all-trivial premises collapse to one path → `AgentRunError` →
  `premise_unverified`.
- **Single choke point**: new `argosy/agents/trader_prompt.py`;
  `assemble_trader_user_prompt` is the only route agent text takes into the trader
  message. Structural headers are module constants; `escape_agent_text` strips
  control/ZWSP/bidi chars and neutralizes marker substrings. URL identity rejects
  ZWSP, RTL overrides and `%0a`/`%0d`/`%00` instead of normalizing them.
- **Integrity is checked BEFORE `trader_hold`**, so an outage can no longer
  masquerade as a considered decision. New `blocked_by` values
  `bull_independence_unverified` and `infrastructure_degraded`; the API returns a
  structured blocked response instead of 500 and enqueues reevaluation. Structural
  retries no longer trip the circuit breaker (they were making later runs look
  like infra outages).
- **The invariant test exists**:
  `test_trader_prompt_agent_text_invariant_via_model_reflection` poisons all 22
  reflected agent-authored fields with a marker plus a counterfeit header and
  asserts zero authoritative headers. `agent_authored_string_fields(...)` grows
  automatically as models add fields — so a future channel gets caught by this
  test rather than by round eleven.

**TEST COVERAGE GAP — do not read "49 passed" as reassurance.** Prior rounds
reported 69 then 73 across five files; round 10 reports **49 across only two**
(`test_bear_independence_and_premise.py`, `test_decision_flow.py`). It did NOT run
`test_fleet_reliability.py`, `test_researcher_debate.py` or
`test_hallucinated_sources_detector.py` — and it **changed circuit-breaker
semantics**, which is exactly `fleet_reliability` territory. Run the full five-file
set before trusting this round.

**Remaining gaps the implementer declared honestly:** only the TRADER prompt is on
the assembler — facilitator, risk and FM prompts are not, so the same injection
class remains open for them (the trader is the one that acts, which is why it was
first); analyst payloads are escaped as a tree rather than by field reflection;
"substantive" is a length/throwaway floor and could be satisfied by padding, since
quality is deliberately left to judgment; live-LLM e2e with both bull and bear
retrieving was not re-run; and UI copy for the two new `blocked_by` values needs a
label pass at merge.

### C — prediction ledger. Six blockers open.
Resolved: Discord archiving no longer deletes scoreable history; missing entry
quotes leave durable rows. Still open: superseding a scored loss erases it from
the denominator (`reliability.py:507-513`); event-time pricing accepts the
same-day close regardless of verdict time, leaking hours of future information
(`event_time_price.py:34-56`); evaluator failure audit is non-atomic across two
connections; abandoned daemon quote workers accumulate unboundedly; backfill
counters cannot reconcile (`versioned` can never increment); the scorecard API
exposes no `total_excluded` / `exclusion_rate`.

**The peer-relative HOLD rule (owner decision 1) is NOT implemented.** The
CSPX-relative work in `hold_benchmark.py` is superseded. Feasibility limits found
this session: the *full* best-peer comparison against an investable universe is
NOT computable — there is no class-tagged candidate registry, no point-in-time
record of what alternatives existed at decision time, and no durable price
history for non-held names (only a 6-hour `kv_cache`). Live-fetching prices would
produce numbers that cannot be re-derived, violating output-trust. What IS
reproducibly computable today is **best peer among same-class names actually
held** (e.g. 12 names in Global quality growth). The wider version needs a
persisted price table first — the same `benchmark_prices` gap the scorecard
design already flagged.

### D — managed/unmanaged holdings. Round 4 in flight (the ingest repair).
Fixed and confirmed by review: lifecycle (missing accounts stay active, explicit
admin-gated closure records reason/actor); degraded propagation all the way
through service → API DTO → TypeScript → UI as "valuation unavailable";
duplicate handling (conflicting duplicates degrade the book, first row kept and
never summed).

Still open at close: stale marks bypass repricing when the symbol is *present* in
a snapshot (`holding_books.py:431-458, 502-504` — a July NVDA row returned
`degraded=False` at $2.307902M with zero quote calls); the admin-token gate
**broke the upload UI outright** (401, 5 failing tests) because `api.ts` sends no
`X-Argosy-Admin` header; rename-before-commit leaves split-brain windows;
rejected overrides leave no durable audit row.

**Corrected NVDA numbers** (the round-3 report's 8,261 was wrong — it hardcoded a
12% target and bypassed the resolver). Re-derived at $180 on the *truncated*
book: tradeable $3.554M, NVDA weight 55.41%, 8% target 1,579 sh, ultimate sell
**9,361 sh**. The executed glide verdict separately specifies a 2026 quota of
**7,304 total / 3,924 remaining**, ending near 1,523 sh. **All of these must be
re-derived once the book is restored** — they were computed on a portfolio
missing $2.38M.

#### Round 4 = the ingest repair. HEAD `6c07ff9`. REVIEWED → **MERGE BLOCKED (6)**

The per-account merge arithmetic is CORRECT and independently verified (46
positions / $4,047.6k = 38 + 8 and $1,615.6k + $2,432.0k). Everything around it
is not. Do not merge as-is:

1. **CRITICAL — merging does NOT restore the money.** `portfolio_snapshot_store.py:170-185`
   merges against the LATEST snapshot, which is already the truncated Leumi-only
   book — so there is nothing to carry forward. Replaying a Leumi feed still gives
   38 positions / $1,615.6k with zero carried accounts. Migration
   `0097_unmanaged_holdings.py:137-191` restores **NVDA only**, leaving 7 holdings
   and **$2.432M absent**. **An idempotent one-off 46-position backfill must be
   written.** Note the data repair and the code fix must land TOGETHER: restoring
   the rows without the merge fix means the next Leumi upload re-erases them.
2. **CRITICAL — stale marks still publish as current.** `/portfolio/snapshot` and
   the allocation breakdown read raw stored rows without `load_total_book`
   (`portfolio.py:223-262, 384-387, 1491-1502`), and `holding_books.py:727-749`
   retains stale values for unpriceable non-policy rows. Probe published both July
   `-` positions ($74.893k) as current with no reprice failure.
3. **CRITICAL — carried quantities are falsely re-dated.**
   `portfolio_snapshot_store.py:255-263` passes the MERGED book (not the feed
   positions) to sync, and `holding_books.py:1160-1188` stamps every row with the
   incoming date. A July NVDA observation became `2026-08-08` after a Leumi-only
   feed — so unobserved Schwab quantities stay "fresh" forever. This defeats the
   whole staleness mechanism.
4. **HIGH — coverage metadata discarded at the API boundary.** It lives only in
   `totals_json`; `row_to_snapshot` omits totals and `PortfolioSnapshotDTO` has no
   coverage fields (`portfolio.py:96-109`). Nothing downstream can distinguish an
   empty account from an uncovered one — which was the point of adding it.
5. **HIGH — rename aliases can DELETE money.** `holding_books.py:312-314, 345-352`
   canonicalizes old and new symbols to one key and keeps the first row. A feed
   containing both aliases worth $100k and $200k produced a single $100k row,
   silently losing $200k.
6. **HIGH — undated feeds bypass the stale-date guard.**
   `holding_books.py:1482-1491` only compares dates when both are non-null. An
   undated $350k feed was accepted as latest after a dated $400k feed, with null
   observation/valuation dates, and `mark_is_stale` treats null as fresh
   (`holding_books.py:182-184`).

53 branch tests + 21 upload tests pass and cover none of the above. UI typecheck
passes; UI lint fails only in files this branch did not change.

#### Round 5 = backfill written. HEAD `e6588bf`. REVIEWER-VERIFIED, still blocked

Round 5 landed despite four subagent-launch timeouts (the launches did run). It
added `scripts/backfill_restored_holdings_book.py`, touched all six blocker
areas, added 357 test lines and plumbed coverage into the UI.

**What I verified MYSELF against a fresh copy of the live DB (not the
implementer's word):**

- Dry-run reconstructs **46 positions / $4047.6k** — the verified target.
- Applying yields all four accounts: `aborad` 1/$69.0k, `leumi` 38/$1615.6k,
  `schwab` 1/$2307.9k, `schwab 876` 6/$55.1k. NVDA 10,940sh, BMY, and **both**
  `-` positions return. New snapshot id 50, `source_path=backfill:last_coverage_restore`.
- **Idempotent**: second run prints `noop`, not a duplicate.
- `observed_as_of` correctly stays `2026-07-13` on carried rows and `2026-08-08`
  on Leumi rows — blocker 3's date preservation works at the backfill level.
- **Cross-account lots are preserved**, resolving my open question: SCHD, VOO,
  SCHG and SGOV each appear as separate rows in BOTH `Leumi` and `schwab 876`.
  The dedup does not collapse them. Whether every downstream consumer *sums*
  them is still unproven.
- `mark_is_stale` / `quantity_is_stale` now return True for a null date, so
  blocker 6's "null reads as fresh" is fixed at the function level. Staleness is
  DERIVED at load time from the dates, not stored — so null flags in
  `positions_json` are not evidence of a bug (I nearly misread this).

**Still blocked. Two new defects I found, plus 15 regressions:**

- **REGRESSIONS — 15 tests that pass on master fail on this branch.** Baseline
  proven: `tests/test_wealth_dashboard.py tests/test_plan_export.py
  tests/test_snapshot_refresh.py tests/test_portfolio_deploy_cash_preflight.py`
  = **85 passed on master `4d97a73`**. Branch selection
  `-k "upload or snapshot or portfolio or wealth or holding"` = **16 failed, 420
  passed** (one, the expense-ingest 400 test, is unrelated). Two are money bugs:
  - **Excluding NVDA removes $23,960 too much.** `test_exclude_nvda_drops_nvda_from_estate`
    asserts `ex.us_situs == full.us_situs - 200_000`; obtained $272,031.01 vs
    expected $295,991.02. So `full` is $495,991.02 and the exclusion stripped
    **$223,960.01 instead of the seeded $200,000 of NVDA** — it drops ~$23,960 of
    non-NVDA US-situs value. Suspected cause (unconfirmed): `exclude_nvda` now
    routes through the unmanaged path and excludes the whole unmanaged set, or
    drops carried/stale rows, rather than the NVDA position alone. Understating
    US-situs exposure understates a tax liability.
  - **Canonical alias leaks into the persisted book.**
    `test_cash_pension_and_unpriceable_rows_carry_quantities` raises `IndexError`
    on `p.symbol == 'מחקה ת"א-200'`. Mechanism verified:
    `normalize_symbol_identity('מחקה ת"א-200')` returns `'ת"א-200'` — the `מחקה`
    prefix is stripped. **No money is lost** (the refresh logs `carried: 6`); the
    stored *symbol* is rewritten. Canonicalization is right as an internal merge
    key and wrong as a mutation of stored data, because it silently breaks every
    symbol-keyed consumer — above all the `instrument_plan_classes` join that
    drives classification, composition, FX and estate. I first read this as a
    vanishing position; it is a rename leak, which changes the fix.
  The implementer verified only its own file (64 pass) plus upload tests. **Always
  run the wider selection before believing a stream is green.**
- **The backfill reports success while failing its core purpose.** Against a
  migration-0094 DB (= production), it logged
  `holding_books.unmanaged_upsert_failed` / `no such table: unmanaged_holdings`
  for NVDA and still printed `restored: 46 positions`. NVDA would come back
  **counted as MANAGED** — the exact distortion the stream removes. Must fail
  closed, non-zero exit, no write.
- **`accounts_carried` is `[]`** while `schwab`, `schwab 876` and `aborad` are
  entirely July-carried. Distinguishing covered from carried is blocker 4's whole
  point, so this is actively misleading.
- **`--dry-run` is opt-in, so the default action is to WRITE.** Invert it for an
  operator repair pointed at the book; keep the `argosy.db` refusal and the
  auto-backup (both good).

Round 6 is in flight with all of the above; regressions outrank the remaining
blockers. A first attempt at the regression round returned "success" having
committed **nothing at all** (HEAD unchanged, empty diff) — always verify a
hand-back with `git log`/`git diff --stat` before believing its report.

**Two worktree gotchas that cost real time:** the worktrees are NOT pip-installed,
so a bare `python script.py` silently imports the MAIN repo's `argosy` package
(this is why `holding_books` appeared to not exist) — insert the worktree root at
`sys.path[0]`. And never name a scratch file `inspect.py`; it shadows the stdlib
and produced a baffling `module 'inspect' has no attribute 'get_annotations'`.
Also note `rg`/Grep skip `.worktrees/` by default because it is gitignored — pass
`--no-ignore`.

#### Round 6 = regressions cleared. HEAD `03b7692`. Adversarial review in flight

**Verified by me, not taken on the implementer's word:**

- Selection `-k "upload or snapshot or portfolio or wealth or holding"` = **1
  failed, 435 passed** (was 16 failed / 420 passed at `e6588bf`). The lone
  failure `test_bug1_rest_upload_returns_400_for_max_without_card_last4` **also
  fails on master** — pre-existing, not Stream D's.
- The test-file edits are legitimate, not test-gaming: deploy-cash monkeypatch
  lambdas gained the new optional `db=None` arg, and three Stream D tests seed an
  explicit `UnmanagedSymbolPolicy` row. No assertion was weakened.
- Migration `0097:123-135` really does seed policy rows, so the integrity gate is
  not silently off for the existing user.

**Mechanisms, now named:**

- The Hebrew-alias regression was `merge_positions_per_account` writing
  `normalize_symbol_identity()` into `d["symbol"]`, mutating the stored ticker.
  Fixed: canonical form is merge-key only, feed symbol stays authoritative.
- The estate "$23,960 over-exclusion" was **not** an unmanaged-set wipe (my
  hypothesis was wrong). Per the implementer it was `load_total_book`
  live-repricing the seeded NVDA from $200k to ~$223,960 (~$223.96/sh). Fixed by
  having dashboard/resolver/net-worth pass `today=snapshot_date`. **Unresolved:
  why a unit test obtained ~$223.96/sh at all, and why that disagrees with the
  $180/sh used elsewhere this session** — handed to review.

**Adversarial review of `03b7692`: BLOCK, 9 findings, every one probe-backed.**
Round 7 in flight. Ordered by "what makes running the backfill on production
unsafe" first:

1. **CRITICAL — `.bak_pre_restore` is not a safe SQLite backup.**
   `scripts/backfill_restored_holdings_book.py:72-82,152-156` uses
   `shutil.copy2`, which omits committed data still in the WAL; the probe saw the
   source holding a committed row the copy lacked. **This is the backup we would
   rely on before touching production money.** Use the SQLite backup API or
   `VACUUM INTO`. The live-DB guard is also filename-only (bypassable via an alias
   or hardlink), and repeated applies overwrite the same backup path.
2. **CRITICAL — surfaces use incompatible valuation clocks, and one is
   temporally mislabelled.** Dashboard/resolver/net-worth pass
   `today=snapshot_date` while `/portfolio/snapshot` uses real today, so the
   dashboard published the July book while `/portfolio/snapshot` degraded the
   same book and refused values — both reporting `snapshot_date=2026-07-13`. The
   dashboard says "as of today" and never renders `assumptions.as_of`. Worse, a
   missing durable holding can take a **current** quote while stamped with the
   **historical** date: `nvda_price=223.96` published as `valued_as_of=2026-07-13`.
   A live price wearing a July date is arguably worse than the regression it
   replaced.
3. **CRITICAL — same-shape updates are silently discarded.**
   `portfolio_snapshot_store.py:527-563`: `latest_matches_snapshot()` compares
   only source path, date and feed count. Probe: incoming `['CSPX','NEW']`
   dropped, stored stayed `['CSPX','NKE']`, `write_result=None`. So a sale or
   symbol replacement with an equal row count is ignored, and "real feeds are
   never dropped" does not hold. **Pre-dates the branch** (exists on master at
   `portfolio_snapshot_store.py:221`) but the branch modifies it and it undermines
   the erasure fix.
4. **CRITICAL — new tenants get no integrity gate.** 0097 seeds
   `UnmanagedSymbolPolicy` only for users existing at upgrade; onboarding
   (`argosy/tenancy/onboarding.py:91-100`) creates none, so
   `load_explicit_policy_symbols()` is empty and a later NVDA erasure is accepted
   silently (probe: `policy=[]`, missing NVDA, `degraded=False`). Confirms the
   concern I raised.
5. **HIGH — net worth publishes totals contradicted by its rows.**
   `net_worth_bases.py:167-189`: probe `position_sum_k=100` with
   `totals_json_k=1000` published **$1,000,000**. Masks truncation instead of
   deriving from auditable rows.
6. **HIGH — money tests reach live quotes.** The estate explanation is CONFIRMED:
   the `$223.96` arrived through the production `default_quote_fn` into the
   yfinance-backed adapter because the test pinned no quote; the `$180` used
   elsewhere this session was an explicitly injected test quote, not a production
   price. Reviewer could not tell fresh hit from adapter cache — either way it is
   nondeterminism inside an arithmetic assertion.
7. **HIGH — the XLS upload path swallows the integrity-gate rejection.**
   `xls_osh_pair.py:288-305,345-379` catches `SnapshotIngestRejected` in a broad
   `except Exception` and returns `[]`, so the upload reports success while the DB
   write was refused; override flags are not forwarded either. **Pre-existing —
   the branch does not touch `portfolio_ingest/` at all** — but it is a live
   bypass of the protection this stream builds.
8. **MEDIUM — no-op backfills misreport carry provenance.** First apply reports
   `accounts_carried=['schwab']`; the second reports `[]` while the values are
   still July-dated.
9. **MEDIUM, pre-existing — ordinary snapshot upload is not admin-authenticated**
   (`portfolio.py:601-620` accepts a caller-supplied `user_id`). Report-only; it
   is Ariel's call whether a local-only API needs this.

**Reviewer's verified NON-findings (these are now settled):** cross-account
aggregation DOES sum correctly (`investable_k=600`, `tradeable_k=600`,
`nvda_pct=50`, `estate_usd=300000`, safety gate agreeing) — this closes open
concern 4 below; the synthetic sale merge removed NKE; read-only production
reconstruction did **not** resurrect NKE, RKT or SPCX; `-` rows stay separate per
account; and reverting either the dashboard as-of fix or the last-coverage merge
made the corresponding regression test fail, so the revert-detectors are real.

**Earlier open concerns (1-3 and 5 now superseded by the findings above; 4 is
CLOSED by the reviewer's aggregation probe):**

1. **Cross-surface consistency.** Dashboard/resolver/net-worth now value as-of
   the snapshot date while `/portfolio/snapshot` values at real today. Two
   surfaces could publish different totals for one book, one degrading on stale
   marks while the other shows July values as current. Output-trust requires
   self-consistency across surfaces.
2. **Integrity-gate fail-open for new users.** `load_explicit_policy_symbols`
   deliberately does not fall back to `DEFAULT_UNMANAGED_SYMBOLS`, and 0097 seeds
   only users existing at upgrade time. A user or tenant created later gets an
   empty policy, so the "policy holding must be present or degrade" gate — the
   protection that would catch a future erasure — never fires for them.
3. **Net-worth heuristic** prefers `totals_json` unless durable restores add
   value beyond snapshot positions. A stored total that is not an independent sum
   over the rows is what output-trust forbids.
4. Cross-account **summation** downstream still unproven (rows survive; whether
   allocation, concentration, the NVDA denominator, estate and net worth each sum
   a ticker held in two accounts is not established).
5. Live production migrate + apply never exercised; blockers 2/4 not verified
   end-to-end against a running server.

### E — async event-loop root cause. Two blockers open; not started.
Fixed in round 2: same-loop `run_coro_sync` fails fast instead of deadlocking
120s; tenant engines on `NullPool`; bridge timeouts and thesis-monitor
degradation reach `/health` and job summaries. Open: systemic non-infra errors
and successful-but-empty feeds still produce healthy summaries (if every quote
returns `None` or every thesis feed is empty, nothing degrades — recreating the
original disease); the process-global counter delta cross-attributes failures
between concurrent jobs and can double-count.

Also: a follow-up agent tasked with auditing the ~60 remaining `asyncio.run`
call sites **produced nothing** (clean worktree, no commits, no report). That
audit is still owed. Sites of particular concern, all running under the live
loop: `plan_synthesis/inputs.py` (~1327, 1459, 1633, 1693, 1832),
`thesis_monitor.py` (~558, 588), `stock_decision/fetchers.py` (~26),
`api/events.py` (~102), `agents/base.py` (~1226). Several carry comments
asserting `asyncio.run` is "safe here" because the caller is in a worker thread —
those claims need verifying, not trusting.

Latent issue spotted but unconfirmed as fixed: in `run_coro_sync`'s nested-offload
path, `with ThreadPoolExecutor(...)` calls `shutdown(wait=True)` on exit, which
on the timeout path blocks on the very worker that just timed out.

---

## 5. Merge hazards — resolve deliberately, do not let git pick

1. **Alembic heads.** `0095` (A), `0096` (C), `0097` (D) ALL declare
   `down_revision = 0094`. Merging as-is creates multiple heads. Create a **merge
   revision** with `down_revision = ('0095_remediation_requests',
   '0096_prediction_ledger_scorecard', '0097_unmanaged_holdings')`, or do pairwise
   merges — do NOT silently rebase one onto another. (If only D lands, `0097` on
   `0094` is coherent against master alone.)
2. **A vs B semantic conflict** at `argosy/agents/researcher_facilitator.py:24-49`
   and `argosy/decisions/flow.py:630-683`. A textual merge will silently lose one
   side. Both implementers independently agreed the resolution, so execute it
   deliberately:
   - Keep **both** model fields on `DebateOutcome`: A's blocking `conditions` AND
     B's `premise_disagreements`.
   - Keep **both** post-facilitator operations in `flow.py`: A's conditions
     handling AND B's `detect_premise_disagreements` →
     `authoritative_premise_disagreements` →
     `model_copy(update={"premise_disagreements": …})`.
   - Do NOT take either facilitator wholesale.
   - Preserve A's override-after-integrity-gate ordering.
   - Trader assembly must keep going through `assemble_trader_user_prompt`; if A
     adds condition text to the trader, route it through the same escape choke
     point.
3. C also changes verdict persistence in `flow.py`; D changes `models.py` and
   portfolio state. `tests/test_decision_flow.py` is touched by A, B and C.

---

## 6. Process lessons (worth keeping)

- **Passing counts prove nothing.** A shipped 8 green tests over 4 live failures,
  then 48, then 57 — while production gathering returned zero. B shipped 69 then
  73 green while a working injection existed. Demand tests that fail on revert,
  and prefer one generic invariant over N per-channel tests.
- **Require the production entry point.** Both of A's fabricated rates came from
  measuring new code or injected fixtures rather than the code that runs. The
  evidence standard now is: state the command, the production function entered,
  and the raw output — and "could not measure" is an acceptable answer.
- **Reviewer must not relay implementer numbers as fact.** This session I
  reported A's 37/38 and "OKLO is now blocked" to the owner; both were wrong.
  Verify load-bearing numbers independently, or label them as claims pending
  review.
- **Two defects can conceal each other.** A's dead gate over dead liveness looked
  survivable only because neither half worked. Ask what would happen if a fix
  *succeeded*.

---

## 7. Open asks on the owner

- **SEC contact email** for `ARGOSY_SEC_CONTACT_EMAIL`. SEC blocks callers with
  no declared contact; it appears in public request logs, so a dedicated address
  is probably preferable. This is the single thing standing between Stream A and a
  real equity provenance measurement (currently 0/13).
- **SEC outage policy** — rule on fail-open-vs-fail-closed (see §4 Stream A)
  before `integrity_vintage_enforce` is ever set true.
- **Schwab cost-basis CSV exports** — Equity Awards Center → "Export Cost Basis",
  one per non-Leumi account (NVDA is in `schwab`, BMY in `schwab 876`). Importer
  (`argosy ingest schwab-lots`) exists, is idempotent, and does not delete on
  partial export; it has never been run. This unblocks the switching-cost band
  and the TaxAnalyst's empty-lots fallback.
- **What is the `-` position in `Aborad`?** 3.0 units, $69.0k, no symbol, no
  proposal, no verdict. Needs identifying before it is restored as a mystery.
- **The Schwab TSV tabs stopped being included.** Per-account merge stops a
  partial file from erasing accounts, but it cannot make Schwab data fresh —
  those positions will carry forward at 2026-07-13 marks, flagged stale. If the
  export changed, that is the upstream fix.

---

## 8. Next actions, in order

1. **Stream D round 5 — the work order below.** This is the money fix. Ariel
   approved one more round; it could not be launched because the session lost
   both subagent creation and shell access (see §9). Hand this to an implementer
   verbatim.
2. ~~Land the ingest repair (Stream D round 4)~~ — **do NOT merge round 4 as-is.**
   It does not restore the money (blocker 1) and its staleness mechanism is
   defeated (blocker 3).
2. **Re-derive everything downstream of the book**: NVDA concentration and sell
   quota, US-situs estate exposure, retirement safety gates, FI shock,
   deploy-cash. Every current figure was computed on the truncated book.
3. **Stream E** — the async root cause. Must not run concurrently with D; both
   modify `snapshot_refresh.py`.
4. **Fix the FX 3.7 fallback** and the duplicate-snapshots-per-date ambiguity.
   Neither has an owning stream yet.
5. **Stream A** — liveness before enforcement; do not switch the gate on at 0%.
6. **Stream B** — independence first, then the prompt assembler and the invariant
   test.
7. **Stream C** — six blockers, then the peer-relative HOLD rule scoped to
   held-name peers, with `benchmark_prices` as the prerequisite for the wider
   universe version.
8. **Do NOT publish a performance scorecard** until prices, FX and
   snapshot-per-date ambiguity are fixed. The original BLOCK verdict stands, and
   this session's findings strengthened it.

## 9. Stream D round 5 — work order, ready to hand off verbatim

Ariel approved one more round on D. It was never launched: subagent creation
timed out four consecutive times and the shell then became unavailable
(`cannot enforce the 'workspace_readwrite' sandbox policy`). File reads and edits
still worked, so this work order was written down rather than half-built. **No
backfill code was written, because it could not have been executed, tested or
committed — an unrunnable repair script pointed at the book is exactly the
unverified artifact this session spent all day blocking.**

Worktree `.worktrees\stream-d-managed-holdings`, branch
`feat/stream-d-managed-holdings-abstention`, HEAD `6c07ff9`. **Never write to the
live `db/argosy.db`** — read it via `mode=ro` or work on a copy; the reviewer
runs the repair on production after review. The per-account merge arithmetic is
CORRECT and independently verified (46 positions / $4,047.6k); all six blockers
are in the machinery around it. Priority order:

1. **Write the missing backfill (blocker 1, CRITICAL).** Merging restores nothing
   because it reconciles against the already-truncated latest snapshot. Build an
   explicit **idempotent** repair reconstructing the 46-position book: dry-run by
   default with an explicit write flag; carries the 8 non-Leumi positions from
   snapshot 34 at `observed_as_of=2026-07-13` marked carried/stale, values from
   live repricing (invent nothing, do NOT stamp today's date — that is blocker
   3); prints post-repair count, total and per-account breakdown confirming NVDA,
   BMY and **both** `-` positions. Prove on a DB copy with pasted output, and
   prove a subsequent Leumi-only feed does not re-erase. Justify migration vs
   standalone script. **The repair and the code fix must land together** —
   restored rows without the merge fix get erased by the next Leumi upload, and
   the merge fix without the repair leaves $2.432M missing.
2. **Blockers 2 and 3 (CRITICAL)** — as documented in §4 Stream D. Enumerate
   every money-publishing surface and prove each routes through
   `load_total_book`; only feed-covered rows may take the feed's date.
3. **Prove cross-account summation.** SCHD, VOO, SCHG, SGOV each exist in BOTH
   `Leumi` and `schwab 876` — legitimate separate lots, not duplicates, and the
   dedup keeps the first row rather than summing. Prove by test that a ticker in
   two accounts sums correctly in allocation breakdown, concentration/weight
   math, the NVDA denominator, US-situs estate exposure and net worth, without
   wrongly degrading the book. **If it under-counts, that is a money bug that
   outranks items 4-6.** The reviewer did not flag this; it is unresolved, not
   cleared.
4. **Blocker 5 (HIGH)** — both rename aliases in one feed is genuinely ambiguous;
   degrade loudly for human resolution, never keep-one (loses money) and never
   silently sum (may double-count). Keep single-alias renames working.
5. **Blocker 6 (HIGH)** — null date means unknown: never fresh, never silently
   the current book. Decide reject-vs-override and justify.
6. **Blocker 4 (HIGH)** — plumb coverage metadata service → DTO → TypeScript →
   UI, as was already done for `composition_unavailable_reason`.

Every fix needs a test that fails on revert, reproducing the reviewer's specific
probe. The existing 53 + 21 passing tests cover none of the six.

## What actually shipped this session

Only the trades, from earlier: **TRLV proposal #20 rejected** (fleet green-lit a
BUY on materially wrong financials — this is the incident Streams A and B exist
to prevent) and **IOVA proposal #19 un-shadowed** and now in the approvable
queue. No code merged.
