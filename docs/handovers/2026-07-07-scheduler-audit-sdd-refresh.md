# Handover — 2026-07-07 · scheduler audit, dead-tier fix, phases A/B/C1 shipped, SDD overhaul, FM-greeting build in flight

**UPDATE (late session, HEAD ~5330c5f + in-flight):** shipped since the morning sections below —
- **Dead loop tier fixed** (`c285673`): registry adopts scheduler-only loops at boot; discovery/weekly-review/backup/monthly-cycle etc. RAN FOR THE FIRST TIME EVER; catch-up staleness guard (7d).
- **Discord listener disabled** (`930909b`, knob `ARGOSY_DISCORD_LISTENER_ENABLED`) — reconnect bug + API blocked; value-review TODO below.
- **Phase A** proactive `period_directive_daily` 19:00 (`c1d27de`, triage-first, fleet authors only when there's a decision); **Phase B** closed-loop verifier (`22da8c9`, `argosy/services/closed_loop.py` — the $161k deploy's 11+1+4 armed expectations parse and verify on next ingest; 7-day sweep → one needs-info item); **Phase C1** smart news intake (`5bf407c`, tickers from the book at tick time, ETFs light, stage-2 fires only on new signals or ≥4% moves).
- **SDD overhauled**: 41-item divergence review applied (`bb988f0` + `b588da2` + `5330c5f`) + **NEW BINDING RULE: SDD is user-AGNOSTIC** (mechanisms, never tenant plan values — memory `feedback_docs_current_state_only` rule 2). §20.3 verified: target pie DOES bind to TargetAllocationDoc (claim was stale).
- **NOTE (Ariel): genericize concentrated-position** — NVDA hardcoded ~148 files; it's "employer equity from RSU vesting", belongs in per-user plan state. No schedule.
- **FM first-greeting home SHIPPED** (`96be8ac` liveness+reaper+flag TTLs; `3be7f19` GET /api/home/greeting; `bee3238` FMGreetingCard leads the page, banner stack demoted to Full detail). Zombie runs 134/135/136 reaped via the endpoint; never-expiring flag backfilled. Home audit had found: 3 of 5 red flags internal/stale; "Synthesis #136 in flight" banner was a dead run (**REAL BUG remains: synthesis dies under the server env — `claude.exe not found at ..._bundled\claude.exe` — investigate!**). Greeting live-verified: book $3.99M, FI 2028 (46), needs_you = the holistic rebalance only.
- **Thesis escalation has teeth** (`382b5e2`): weakened/broken thesis_monitor flag elevates a name into the daily holdings-review deep pass regardless of size; evidence feeds the agent's context; `elevated` in the job summary. Remaining ladder piece: **un-shadow the decision funnel's graded verdicts as labelled beta** (nothing-hidden rule) — QUEUED, touches inbox UI.
- **MONEY-PATH BUG found live + fixed** (`b98148c`, `9a539d0`): fills-applied/self-refresh rows carried `allocations_json` forward VERBATIM → the post-deploy row still showed pre-deploy Cash 170.98k/delta −98.28k → the cash detector fed the directive fleet a phantom $98k excess → **proposal 47 recommended deploying already-deployed money**. Allocation block now re-derived from the row's own positions. Live-corrected through the real paths: fresh self-refresh row (Cash current $9.61k), directive triage re-ran → quiet skip + **superseded proposal 47**. Lesson reinforced: derived tables carried forward are lies-in-waiting; re-derive from raw.
- **10 FIX-CODE divergence items**: #2 directive-push DONE (phase A), #1 closed-loop DONE (phase B); remaining: daily-brief scheduling+event, watchdog alert wiring, domain-refresh due-date scan, offsite backup config, pin expiry window, gap_due emission, funnel graduation counter, plaintext external_api_keys fallback.
- **Full suite**: re-running (`tmp/full_suite_2026-07-07.log`) — slow (machine busy); yesterday's 3 unidentified failures still to name.
- **Watch tomorrow**: 04:00 sweep (closed-loop day counter), 17:00 news (first real RSS fetch, stage2_gate reasons), 17:30 holdings review, 19:00 directive. verify-run after the first full cycle. Test runs pollute logs/app/application.log — give tests their own sink (open item).

**Branch:** `master` · **HEAD:** `401682c` · tree clean.
Read this first in a fresh session, then `git log --oneline -15`. Prior: `2026-07-06-resume-fixes-multi-turn-parse-and-detector.md` (funding UI, multi-turn parse fix, detector grounding) and `2026-07-06-fleet-live-e2e-and-plan-v67.md` (plan v67, the $161k live deploy).

## 1. Shipped

- **`3162380` fix(jobs):** `pending_reevaluation_daily` refused the scheduler's clock kwarg (`Scheduler` calls `loop.tick(now=self.clock)`; the loop declared `tick(self)`), so **every scheduled fire raised TypeError since registration** — the pending-reevaluations sweep (the CELH insufficient-data auto-retry path) never ran on cron. Fixed + a regression test pinning the scheduler calling convention.
- **`401682c` docs(sdd):** SDD folded up to current state (~30 commits of drift): self-refresh/apply-fills, funding breakdown, fleet reliability + max_turns=3, session isolation/telemetry, `_select_response_text`, WebSearch analysts + anchors, grounded hallucination detector, inbox 0077/auto-supersede, x10 mandate + domicile carve-out, plan refine/rollback routes, verify-run, and a 13-job scheduler roster table (§5.1). Current-state-only; each claim code-verified by the doc subagent before writing.

## 2. Scheduler audit findings (Ariel asked "is the scheduled review active and working?")

- **The scheduler is in-process — no backend, no reviews.** Backend is DOWN (nothing on :8000); apart from a brief start on 2026-07-06 morning, **no daily job has fired since 2026-07-03**. Discord listener (long-running) ran through Jul 6. If reviews should run regardless of dev sessions, the backend needs persistent hosting — Ariel's call, not made yet.
- **Discovery is NOT a scheduled job** — on-demand only (plan-synthesis sourcing / Explore). If Ariel expects a cron discovery review, that's a feature decision, not a breakage.
- `thesis_monitor_daily` last failed Jul 3 on the claude.exe exit-1 (pre-max_turns-fix) — expected to heal on its next fire; **watch its first post-restart run**.
- `holdings_review` (daily 17:30 IDT per-stock research→verdict) has **never fired** (registered correctly; backend never up at its cron time since it shipped).
- Jul 3's `news_daily`: fetched 2, analyzed 0 — looks like thin volume; verify on the first post-restart run.

## 3. Full-suite post-mortem (killed run)

Launched 2026-07-06 ~20:57 over `229ce99`; log (`tmp/full_suite_2026-07-06.log`) advanced to **26% by 21:12 with 3 failures around 24%**, then stalled (likely a hang — cf. the known real-claude.exe-call-in-tests class); the laptop slept at 23:37 and the harness reaped the background task. Nobody stopped it manually. **The 3 failing test names are unknown** (quiet mode never reached the summary). Re-run needed: use `-v` (or `--timeout` via pytest-timeout if available) so failures name themselves and a hang self-identifies; machine must stay awake ~3.5h.

## 3b. Evening session — FM home, charts, and three deep root causes (HEAD ~ea6d85b)

- **FM greeting home iterated with Ariel live** (3 rounds): hero folded (logo + salutation top panel), duplicate panels merged/demoted, windfall banner off home, factful watching headlines, action items verified-with-evidence → needs-confirm entries, quarterly wealth trajectory (BOOK universe, rate-anchored projection, "history begins Mar 2026"), deconcentration chart (canonical basis + real glide waypoints as dated chips), plan-adherence panel (live status + critique age + next auto-review), human plan names ("Plan v67 · x10 sleeve · Jul 2026").
- **Chart bugs root-caused**: $4M→$4.8M jump = book actuals joined to total-net-worth projection (universe mismatch); deconcentration "5%→11%" = naive name-match rendered the "**ex-NVDA**-dense" class as the NVDA glide; NVDA % basis unified to canonical `nvda_concentration_pct` (tradeable book).
- **"Claude Code not found" ROOT CAUSE (`3b9ae78`)**: NOT a missing exe — **Windows 32,767-char argv cap** (WinError 206 → Python FileNotFoundError → SDK's misleading message). plan_synthesizer's 17.2k system prompt + 12.8k schema blew it; small-argv agents were fine. Fix: >8k system prompts go via `--system-prompt-file` (temp file, 24h self-prune). Deterministic repro test; live >8k call not yet exercised.
- **plan_critiques was EMPTY FOREVER — second root cause (`ea6d85b`)**: graph-cutover plans carry `raw_markdown=''` so weekly_review no-op'd ("no_plan"). Fix: `plan_export.plan_markdown_for_review` (authored prose else canonical export) wired into weekly_review + POST /api/plan/critique. **First-ever critique landed live** (row 1, v67, Opus): confidence LOW, **2 RED** — (1) Wealth-Dashboard NVDA 8%/57.3% vs IPS prose 12%/62.5% cross-surface contradiction; (2) dashboard FX≈3.00 vs plan 2.944 (~₪2.34M gap) — **OPEN FOR ARIEL/TEAM**; 9 YELLOW (stale provisional holdings, units mislabel), 3 GREEN.
- **Attribution fix (`b1b9b9d`)**: plan-named instruments attribute to their plan class before asset_type (RXRX/OKLO/TEM → moonshot sleeve 0.97% vs 5.43% target); redeploy residual = true legacy singles (GOOG/AMD/AMZN/RKT/SOFI/META/TSLA, 18.0%).
- **Money-path saves**: phantom $98k directive (stale allocations table → fixed `b98148c`, superseded via triage) and buy-NVDA rebalance legs (concentrated sleeve now excluded from buy universe `44dde67`, proposal 48 superseded; corrected run proposes NOTHING).
- **NVDA pace live: BEHIND** — 1,600 sh sold YTD vs glide flow ~4,775 (weight measure is ON SCHEDULE vs due waypoint 59.5% at 57.3%) — **pace-vs-waypoint adjudication = open fleet question** (the directive's sell-half).
- **Full suite: HUNG at 14%** (845 passed, 0 failed) — culprit = first test of `tests/test_api_phase4.py` (never completed; scheduler-thread/shared-engine hang class). Killed. Next: run that file alone under a timeout, fix, then overnight full suite with pytest-timeout.
- **New opens**: catchup `KeyError: 'weekly_review'` startup race (19 log hits — loop catch-up racing registry adoption); critique cited 2 domain_knowledge files not in its supplied sources (input-plumbing gap, now a real signal post-detector-fix); per-waypoint planned sale sizes (plan synthesis); auto-enqueue critique on material change (state observer); look-through concentration history.

## 4. Open items

0. **NOTE — refactor needed (Ariel, no schedule set): genericize the concentrated position.** NVDA is hardcoded across ~148 files (constants like `NVDA_TARGET_PCT` in `allocation_plan.py`, look-through maps, prompts, UI copy) but it is just the current tenant's instance of "concentrated employer-equity position from RSU vesting (not purchased by choice)". Target shape: the concentrated sleeve (symbol, target, cap inputs, situs exception, glide waypoints) lives in per-user plan state (DB `TargetAllocationDoc`); code reads "the concentrated sleeve", never a ticker literal; prompts get the symbol injected; the `NVDA_*` constants die. Staged: money-path → prompts → UI. Also blocks multi-tenant; root cause of the SDD user-content leak.
0b. **NOTE — Discord listener disabled (2026-07-07):** reconnect bug + Discord blocked the API; 0 signals since 2026-05-29. Review its VALUE before fixing; re-enable via `ARGOSY_DISCORD_LISTENER_ENABLED=1` only after both.

1. **Full suite:** re-run to completion; identify the 3 failures + the hang point (§3).
2. **Backend up** → today's cycle fires (thesis monitor 09:00, news 17:00, state observer 17:00, holdings-review's FIRST EVER run 17:30, alpha 18:00, funnel 18:30 IDT) and the fixed `pending_reevaluation_daily` (04:00 IDT) finally sweeps → CELH should auto-retry with anchors + WebSearch + the parse fix. **verify-run after the first cycle.**
3. **Bank DPYA reply** → IWDP→DPYA swap + $5k property top-up (fallback: top up IWDP as sleeve instrument).
4. **Residual cash ~$9.6k** (+ glide) → next tranche, EXUS-first.
5. **No new broker export yet** — closed-loop expectations still ARMED (8 new positions, CSPX 240sh, EIMI 650sh, SGOV@Leumi 850sh; SGOV fill price is an estimate pending the broker print).
6. **Queued fleet work:** SGOV→IB01/IBTA migration; consolidation batch (XZEW/VOO/SPMO/QQQM/SCHG→CSPX/CNDX vs META/RKT losses); cap-vs-equal-weight US-core adjudication; AI-correlation as moonshot-sourcing input.
7. Israeli feed-less funds (TA-200/MSCI-World-MTF/IBI-STOXX) carry-only in self-refresh.

## 5. Discipline

Team-over-gates held (both fixes were plumbing). Blind adversarial review continues to pay (yesterday it caught the retry-gate blocker). Telemetry-first debugging: the scheduler audit came straight from `cadence_state`/`job_runs`, not guesses.
