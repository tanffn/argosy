# Handover — 2026-07-11 · external implementer work order (close all opens)

**Addressed to:** an implementing LLM agent that is NOT Claude Code. **Review contract:** the
resident Claude Code session is your REVIEWER — you implement, it code-reviews and verify-runs
your output. **BINDING after a same-day incident: work on a branch (`feat/opens-2026-07-11`),
NEVER merge to master yourself, hand back per block.** (The stream-A agent self-merged today;
its post-merge review is in flight — see §4.)

## 0. Read first (you have no auto-loaded memory — these ARE your memory)

1. `D:\Projects\financial-advisor\CLAUDE.md` — router + binding preferences (they apply to you).
2. `C:\Users\ariel\.claude\projects\D--Projects-financial-advisor\memory\MEMORY.md` — index; then
   from the same folder at minimum: `feedback_verdicts_defended_not_reopened.md`,
   `feedback_fleet_authors_determinism_verifies.md`, `feedback_escalation_bar_fatal_forks_only.md`,
   `feedback_argosy_prime_directive.md`, `feedback_output_trust_doctrine.md`,
   `feedback_adversarial_review_must_re_derive_blind.md`, `feedback_never_exit_winners_on_price.md`.
3. `docs/design/SDD.md` §"Quickstart for new agents" (task→file router) + §14.6 (testing incl.
   the calibration benchmark).
4. This file, then `git log --oneline -40`.

## 1. Environment + discipline (violations = review rejections)

- Windows, PowerShell (`;` not `&&`); venv `.venv/`; tests
  `.venv/Scripts/python.exe -m pytest -m "not llm_eval" <path>`; console cp1252 →
  `PYTHONIOENCODING=utf-8` + durable side-effects BEFORE prints.
- Backend runs DETACHED (Start-Process; logs `tmp/uvicorn_detached.*.log`); dev DB `db/argosy.db`
  (shared — 60s busy timeout on writes); UI dev on 1337 (`D:\IDEs\nodejs\npm.cmd run dev` in ui/).
- Scheduler loops use CRON never interval (interval re-anchors on restart and starves).
- Migrations: additive Alembic only; CHECK the latest number under `alembic/versions/` first
  (0082 landed today via the stream-A merge).
- Plan mutations go draft → gated `POST /api/plan/draft/{id}/accept` (pattern:
  `scripts/apply_*.py`); NEVER edit a current plan_versions row in place; NEVER override a
  failing gate.
- Every number auditable to raw rows; the fleet AUTHORS judgment, determinism only verifies
  arithmetic/structure; verdicts ship with conviction + falsifiers + the clock and are DEFENDED
  (a re-run needs a NEW fact hitting a falsifier).
- Commits per logical block. Do not touch: `evals/` scored runs, inbox/plan data rows beyond
  your assigned items, `.worktrees/`.

## 2. Current state (verified 2026-07-11)

Plan **v77 CURRENT** (v74 gold→EXUS · v75 two-lane growth sleeve 2% x10 + 3% alpha · v76
dry-powder earmark · v77 no-price-exit rule). **Draft v78 exists and is correctly BLOCKED** by
the headline gate (stale ₪69,324 FI margin after 2026-07-10's ~$180k of executed trades) — see
work item A. Stance registry live (`position_stances`, /api/positions/thesis is a projection).
Discovery gates wide (MEDIUM floor, $30B radar, 16:00 IL cron); first cycle produced CMPS/VOR.
Stream A (gov-contracts) merged to master today (review in flight). Calibration benchmark:
`evals/fleet_calibration/`, 28/29, protocol + 5-agent workflow in
`docs/design/fleet_calibration_benchmark.md`. Ariel's inbox (NOT yours to touch): rows 72
(sleeve ladder + NVDA-sale funding flow), trade cards 13/14 (IWDP→DPYA switch), 15/16
(CMPS/VOR, expire 2026-07-13).

## 3. WORK ORDER (priority order; one block = one hand-back)

**A. Post-trade plan refresh (GATING).** The book changed ~$180k on 2026-07-10; horizon prose
carries stale headlines (the gate correctly blocks re-publication). Run the corrective
refresh path (SDD §synthesis; corrective machinery attaches corrections automatically) so a
fresh draft re-derives all headline numbers from the current snapshot; promote through the
gate (no overrides). THEN re-apply the guidance-flip mandate addendum: re-run
`scripts/apply_guidance_flip_convention.py` against the new current (edit its version
assertion). Acceptance: new current plan, gate passes clean (headline_numeric_source=0),
guidance-flip text present in the high-growth class rationale, sums 100.00.

**B. Verdict registry + pushback gate (the defended-verdicts directive as machinery).**
(1) `verdicts` registry table (additive migration): verdict, conviction, falsifiers_json,
next_validation, source run, settled flag — written by deep-decision runs and adjudications;
(2) pushback gate at every re-adjudication entry point: a re-run on a settled subject requires
a cited NEW fact hitting a recorded falsifier, else the entry point returns the standing
verdict; (3) blind valuation re-derivation (live data) mandatory inside deep-decision BUY runs;
(4) deterministic sleeve-fit check: a buy proposal must name the plan sleeve that hosts it or
fail structural validation. Acceptance: unit tests for each; a synthetic pushback without new
facts gets DEFENDED; the run-166 NOW failure class replays as blocked.

**C. Retract-on-reversal in the orchestrator.** A decision run whose verdict contradicts an
OPEN proposal on the same ticker cancels that proposal in the same transaction
(proposals_history note citing the run). Three hand-cleanups happened on 2026-07-10
(proposals 2/3/10 — copy their history-note shape). Acceptance: test seeding an open sell +
a HOLD re-adjudication → proposal cancelled atomically.

**D. deploy-cash reads the dry-powder earmark.** Plan v76+ carries
`discovery_reserve` on the cash class; the deployment author/packet must subtract it from
deployable cash (it is currently prose-only). Acceptance: deploy plan over a book with the
earmark shows reserve excluded + labeled.

**E. Signal streams B → D → C → E** per `docs/design/early_signal_streams.md` §4/§6 (contract
§3 already implemented by stream A — reuse it). One stream per hand-back; entry prices
snapshotted AT WRITE on ledger predictions; per-stream ledger sub-sources (stream C:
per-person). **UNBLOCKED — stream-A post-merge review PASSED all 5 dimensions (2026-07-11:
spec contract, migrations 0082/0083 additive + at head, 138 tests green, live cycle verified
end-to-end with 16 entry-priced ledger rows, dedup + failure isolation proven against real
production failures).** Adopt its patterns verbatim. ONE review follow-up folded into this
item: the recipient-resolver's `agent_error` tombstone is permanent — a transient LLM failure
hides a real public recipient forever; make agent_error tombstones retryable/TTL'd (keep
`not_public` permanent).

**F. Benchmark five-agent pipeline + case batch** per
`docs/design/fleet_calibration_benchmark.md` §2a: separate sanitizer / review / grading agents
over the persisted replay trail; then the case backlog (obscure small-cap failures,
trap-shaped winners, winner-shaped failures, AMD-2016 F1, CVNA-2023 re-entry, fresh
synthetics). Real-LLM points are `llm_eval`-class — coordinate with the reviewer before
burning calls.

**G. Alpha-report loose ends.** (1) the analyst fan-out writer still writes
`entry_price=NULL` predictions — snapshot at write; (2) confirm `run_reevaluation_batch` is
wired into the `predictions_evaluator` loop (stream-A merge may have done it — verify, don't
assume); (3) July's 62 extractions score from 2026-07-13 — check they scored.

**H. Fleet evals owed:** RKT alpha-lane re-entry evaluation (it fails the moonshot gate at
~$40B but is admissible in the 3% alpha lane; position fully sold 2026-07-10, Israel has no
wash-sale rule) + NOW 75-share alpha-lane evaluation. Each lands as ONE needs-confirm inbox
row with verdict + conviction + falsifiers + clock.

**I. Carried backlog** (smaller, pick off after A-D): alternatives_phase reads settled
records + fleet-authors its sleeve pct; patch/sliced synthesis flags default-ON after a live
acceptance; in-product zigzag mechanism; run-149 crash diagnosis; cost-cap resume semantics;
synthesizer emits `{{fact:key}}` tokens; NVDA avg_price per-lot rebuild; Schwab equity ingest
path; BRK/B slash-ticker data bug; ips.no_current_plan keying; test_api_phase4 hang (then full
suite with pytest-timeout); catchup KeyError race; backend service wrapper (auto-restart);
degraded_to_monolith in a user-facing DTO; BRK.B productive-ballast plan question (fleet
adjudication → inbox row).

## 4. CLAUDE CODE REVIEWER SESSION — your side (fresh-session start here)

You are the RESIDENT session: Ariel's interface + the reviewer of the external implementer's
hand-backs. HEAD at this section's last update: `49cf456` (= master; item-F block 1 merged,
domain stamps, reviewer flags — see §5 for the live state of items A and F). You do NOT
execute items A-I yourself unless the implementer stalls and Ariel redirects (2026-07-11:
Ariel redirected item A's CLOSE-OUT to the reviewer — run 193 in flight, see §5) — your work:

1. **Review each hand-back on `feat/opens-2026-07-11`** against the §3 acceptance criteria:
   code review (spec compliance, no gate overrides, migrations additive + numbering — 0083 is
   the latest, stream-A took 0082/0083) + tests green + `verify-run` on any live cycle it
   produced. On PASS, YOU merge to master (the implementer never merges — binding). On FAIL,
   findings back with file:line evidence. Re-run the calibration benchmark
   (`evals/fleet_calibration/`, `--dry-run` first) if the hand-back touched any trader/mandate
   prompt (SDD §14.8 eval-first).
2. **Time-critical watch:** CMPS/VOR inbox cards **expire 2026-07-13** — if Ariel hasn't
   decided by the 12th, surface a reminder with the clock sections (both cards carry them).
   July's 62 alpha-report predictions start scoring 2026-07-13 — verify they score (item G3).
   Item A (post-trade refresh) unblocks draft v78's guidance-flip re-apply — verify the gate
   passes CLEAN, never via override.
3. **Ariel's pending decisions** (his, not yours): row 72 (sleeve ladder to 8% + per-NVDA-sale
   funding flow), cards 13/14 (IWDP→DPYA switch, ~$63k), 15/16 (CMPS/VOR $23k each).
4. **Session disciplines that bit this week:** subagents that park "waiting" get resumed via
   SendMessage with finish-synchronously instructions; backend runs DETACHED
   (Start-Process, logs tmp/uvicorn_detached.*.log) — never as a session background task;
   60s DB busy-timeouts; domain-refresh stamps → own chore commit; verdicts DEFENDED
   (new-facts test before any re-run); one voice per position (stance registry is canonical —
   any two surfaces disagreeing is a bug to trace, not explain away).
5. **Stream-A review verdict (2026-07-11): PASS all 5 dimensions, master releasable** — its
   patterns are the blessed template for streams B/D/C/E; the tombstone-TTL advisory is folded
   into item E.

## 5. In flight / coordination

- **Item F block 1 REVIEW: PASS (2026-07-11, reviewer session) — merged to master.** Commit
  `9413276` (five-agent pipeline). Evidence: focused tests 64 pass (`test_suite_live` correctly
  `llm_eval`-marked); score.py refactor regression-checked — regenerated report from the
  immutable scored run is identical in every score + the 28.0/29 headline (only additive
  `source=` provenance tags); stage 4 provably never sees the answer key; stage-3 payload
  carries no real name/outcome; immutability now enforced in code; all agents default Opus.
  **Five follow-ups folded into item F block 2 (fix with the case batch):**
  (1) sanitizer's source manifest comes from the CLASSIFIER's sourced_facts (stage-1→2
  correlated-failure channel — give stage 2 raw-source access or an independent manifest);
  (2) resume on a legacy unreported run stamps `pipeline_version=1` and retroactively
  disqualifies its OK points at score time; (3) delete/fix dead `build_classifier_receipt`
  (emits a role string `verify_classifier` rejects); (4) `prepare_classifier.py` hard-raises
  mid-batch on existing receipts (skip-and-report instead); (5) eval tests aren't collected by
  the default suite (`testpaths=["tests"]`) — add the path or a collection hook. Plus: add a
  direct test that stage-3's payload excludes `real`/`expected_classes` (currently enforced by
  code shape only).
- **Item A (reviewer-owned close-out, in flight):** draft 80 failed the accept gate on 5
  violations (2 checker false positives). Reviewer persisted them as proposal 73
  (`critique_resynth:ariel`) after a dry-verified harvest, launched corrective run 193
  (phases 1-2 reuse, corrections attached, $60 cap, no backend contention, process-level
  watcher). Implementer findings filed: (a) accept-gate 422 is not persisted anywhere
  `build_corrective_context` reads — close the loop; (b) headline_numeric_source misparses
  duration-after-age phrasing ('...(60): 13 years' → "age 13"); (c) history_leak regex flags
  legitimate technical usage ("superseded by the operative glide"). Also: run-191 post-mortem
  = cost-cap kill at $20.95 + two silent backend deaths + 1h47m unmonitored gap → item-I
  reliability fixes (wrapper/cap-resume/stall-alert) now have hard evidence; patch/sliced
  flags flip ON once a corrective run promotes clean (193 is the candidate); Opus
  plan_synthesizer malformed-JSON retries dominate synthesis wall-time (runs 192+193) —
  Fable-5 upgrade candidate, benchmark-gated.

- **REVIEWER FLAG for item A (2026-07-11 ~13:00, pre-hand-back):** run 191 (plan_revision)
  completed 10:19 → draft 79 (`synth-2026-07-11-1019`), not yet promoted. Its backend log
  (`tmp/uvicorn_detached.item_a_reap.err.log`, 09:40:53Z) shows
  `nvda_sales_history.shares_sold_from_schwab_csv total: 0` — the detached backend likely
  started WITHOUT `ARGOSY_EXPENSE_SAMPLES_ROOT`, so the YTD-NVDA-sales input may have been
  empty. Draft 79's headline "2026 quota remaining: 3,924 sh after YTD sales" must be shown to
  derive from real Schwab sale rows (YTD sales are known > 0) BEFORE promotion; if the input
  was empty, restart the backend with the env var set and re-run the corrective refresh.
  Acceptance for item A now explicitly includes this check.
- **Observed implementer state (reviewer, 2026-07-11):** item A mid-flight (draft 79 pending
  gate + promotion + guidance-flip re-apply via `tmp/apply_guidance_flip_convention.py`);
  item E stream B in progress on `feat/early-signals-b` (9 commits + uncommitted WIP in
  `.worktrees/feat-early-signals-a`, last commit 12:51). No hand-back yet on either.

- **Stream-A post-merge review** (Claude Code side): code review + live-cycle verify-run on
  master — its verdict may add fix items to §3.E; wait for it before signal-stream work.
- Hourly domain-refresh job re-stamps `domain_knowledge/*` — commit those stamps as their own
  chore commit if they land in your tree; never mix them into feature commits.
- The reviewer runs `verify-run` on any live cycle you produce and the calibration benchmark
  on any trader/mandate prompt change you make (SDD §14.6/§14.8 — eval-first is mandatory).

## 5b. LATE-DAY STATE UPDATE (2026-07-11 evening — supersedes stale bullets above)

**Git:** master = `b370b3f` (item-F blocks 1/2a/2b all reviewed + merged; 2b needed one FAIL
round — 3 packets had fabricated/look-ahead data caught by adversarial audit vs raw sources,
rebuilt + re-audited, the CVNA price dispute adjudicated in the implementer's favor via the
2026-05-07 forward 5:1 split; amd_2016_f2 equity-sign error found + fixed on the way).
**Item-F burns are GO**: receipts for all 7 new packets, live suite on a NEW --out path,
--dry-run first, §2b table with per-case stage-4 groundedness.

**STREAM B REVIEW VERDICT: FAIL — six blockers, do NOT merge** (full findings in the review
transcript; hand-back sent). B1: ships `enabled=True` by default (config.py:537 + example
yaml + SDD line) — would go LIVE on the household DB at the next 15:30 tick after merge; flip
default off. B2: the BINDING tombstone-TTL follow-up (§3.E) is NOT implemented —
contracts.py:279-340 still permanent `agent_error`. B3: fix#2 incomplete — collaterally
tainted sibling groups stay dead after a correction restores only the resolved group
(live-probed). B4: fix#4 hole — same-pool 10b5-1 SELLS skip without contaminating →
stake_sale_pct inflates to 100% (false C-suite-panic warnings; live-probed). B5: no
per-filing failure isolation on the GLOBAL daily path (one malformed filing aborts the day;
no 429/503 retry). B6: outage-safety is decorative — `fetch` ignores `since`, catch-up knobs
dead, gap days never ingested. What PASSED: fixes #1/#3/#5 with replay tests, fair-access
pacer, entry-price-at-write, idempotent rerun, fail-closed identity (not griefable),
migrations 0084/0085 safe at head, SDD section user-agnostic. Each blocker fix needs a
replay test at the stream-A bar.

**Item A (reviewer-owned): corrective run 196 IN FLIGHT** (draft 82 superseding). The saga so
far, for whoever picks this up: draft 80 failed the accept gate (5 violations, 2 checker
false positives) → proposal 73 bridge (accept-gate 422 is not harvested — filed) → run 193
draft 81 FM-rejected on `[derivation pending]` leaks (no derived-fact key for the gate's
shock values — filed) → run 194 draft 82: FM CLEARED the qualitative narrative but the
whole-artifact reader BLOCKED, misapplying the figure-ban to the machine-rendered FX scenario
table, + 3 REAL findings (conflicting allocation sets, glide share-count, SGLN guardrail).
KEY DESIGN FACT: a reader BLOCK is deliberately wired through
`decision_run.fund_manager_decision='rejected'` and gates /accept (orchestrator.py:1489-1496)
— never "fix" that column, clear the reader instead. Run 196's harvest leads with a reviewer
RULING (scenario tables exempt, narrative wording preserved) + the reader's 3 real findings.
On completion: POST accept (expect clean), re-run guidance-flip (tmp/apply_guidance_flip_convention.py,
update its version assertion to the new current), verify-run, close item A, then flip
patch/sliced flags ON (precondition met by the clean promotion).
HARVESTER LESSON (filed): verdict-feedback re-feeds the CHALLENGER's framing and the
landed-check suppresses the reviewer's corrected framing unless given a fresh topic —
cross-reference memory `feedback_verdicts_defended_not_reopened`.

**New implementer findings filed today (fold into item I):** shock-value derived-fact keys;
accept-gate-422 persistence into corrective harvest; verdict-feedback scope-guard didn't
harvest draft 81's FM rejection; stale `fund_manager_decision` semantics documented (by
design, not a bug); headline_numeric_source duration-after-age misparse; history_leak regex
false positive; Opus synthesizer malformed-JSON retries dominate synthesis wall-time (runs
192/193 burned 3×8min each) — Fable-5 upgrade candidate, benchmark-gated; reliability trio
prompt ready (migrations start 0086 — 0085 is stream B's).

## 5c. FINAL STATE (2026-07-11 night — supersedes §5b where they differ)

- **STREAM B MERGED to master (`53820d1`)** — B1-B6 re-review PASS, replay tests at the
  stream-A bar, default OFF (opt-in), post-merge smoke 142 green. Non-blocking follow-ups
  carried: (1) `_parse_daily_form_index` hard-raises on malformed non-Form-4 index lines /
  odd accession basenames — interacts with B6 (a permanently malformed index day can wedge
  the stream up to 31 days; fail-loud, no bad data, but an availability hole); (2) rare
  fail-open edge in the ownerless-amendment obsolescence rule (same-issuer same-day sibling
  originals) — wants a fixture; (3) stale UA example in
  `domain_knowledge/data_sources/sec_form4.md`; (4) SDD line 305 carries a pre-existing
  user-agnostic violation (out of stream-B scope). Tombstone TTL default 24h — Ariel accepted.
- **Item F CLOSED** (blocks 1/2a/2b + burns merged; 4.0/5 scored, boot_2017 = first genuine
  in-class miss = restored discriminating power). Deferred to a token-flush day: sdc_2021
  re-burn, omk re-score after null-normalization, Fable-5 A/B (first-wave roles), hard-case
  pool growth (n>=5 before any doctrine candidate).
- **Item A NOT closed — deliberately parked (token freeze).** v77 current; draft 85 pending
  (= draft 84 + the FM's durable-allocation reconciliation, 60 values aligned + '5pp' fix).
  Draft 85's accept 422 finally exposed the TRUE deterministic residue: section_coverage
  11/18 (corrective chain silently LOST 6 sections vs v77), unqualified 'reached' sentences
  on section/short surfaces the corrective loops never touched (the surgical-converge class),
  and the tax-event currency pin. Close path: land the synthesis-aftermath block (§5b prompt,
  esp. items 1-4), then ONE corrective run seeded with the 422 list, accept, guidance-flip
  (tmp/apply_guidance_flip_convention.py, assertion → new current), verify-run, patch/sliced
  flags ON. Do NOT hand-author plan content to force the close.
- **Queued on token freeze:** ORCL discovery review (creator thesis as input signal, fleet
  re-derives blind, one needs-confirm row) — fire only on Ariel's explicit go.
- **Ariel decisions pending:** CMPS/VOR (cards 15/16) EXPIRE 2026-07-13 15:30 — remind on the
  12th; cards 13/14 (07-17); row 72. July's 62 alpha predictions score from 07-13 (G3).

## 5d. NIGHT CLOSE (2026-07-11 — supersedes §5c where they differ)

- **ITEM A CLOSED — plan v89 CURRENT.** After run 197 (draft 84 FM-rejected on the NVDA
  triple-endpoint), the close was fully deterministic, zero LLM: v85 = FM's durable-allocation
  reconciliation (60 values, FM-enumerated set); v86 = 4 lost sections restored from the
  chain's freshest drafts + capital-sufficiency sentence qualifiers + estate-clause ₪
  equivalence cue; v87/v88 = remaining unqualified 'reached' sentences fixed via a LOCAL
  gate-loop (`_run_plan_output_gate` in-process until clean); **v88 accepted 200 (no
  overrides, warn-only evidence_per_section=3), v89 = guidance-flip re-applied, accepted.**
  Scripts: tmp/reviewer_close_step3/4*.py. Residual risk (flagged to Ariel): the final
  qualifier splices are gate-driven, reader never re-blessed those two instances; the weekly
  critique of v89 is the independent check — READ ITS FIRST RUN. verify-run was skipped
  (token freeze) — run it when tokens allow.
- **Stream B pushed** (with master + feat/opens-2026-07-11 to origin). All merged branches
  deleted; only master + feat/opens remain. `.worktrees/feat-early-signals-a` DIR is
  file-locked (branch deleted) — manual delete pending.
- **ORCL verdict recorded (run 198): HOLD/wait** — fair value ~$150 vs ~$140.6 (7% buffer,
  leverage-discounted), TTM FCF ≈ −$24.5B, D/E ~3.0, OpenAI backlog concentration. Revisit
  triggers: price ~$110–115 or FCF turns positive with debt stable. DEFENDED verdict — seed
  row for item B's verdicts registry.
- **OKLO / RKLB / ASTS discovery evals fired** (sequential, long_hold T2, no thesis seeded)
  — check decision_runs ≥199 + inbox for verdicts.
- **Ariel decisions:** CMPS (MEDIUM) — STARTING a position (his call, benchmark-consistent);
  VOR (LOW) — letting it expire (expiry ≠ verdict; re-eval later if falsifiers stay quiet).
- **Parallelization (owner question answered):** item B (verdict registry + pushback gate,
  §3.B) runs IN PARALLEL with the §5b synthesis-aftermath block — disjoint code (decisions/
  flow + new table vs orchestrator/quality/evals). Give item B to the freed STREAM-B agent
  (it knows the ledger machinery). MIGRATIONS: aftermath block = 0086, item B = 0087.
  Seed the registry with: ORCL (run 198), the run-186/187/188 HOLDs (SOFI/BMY/OPEN), VOR
  post-expiry, and OKLO/RKLB/ASTS verdicts when they land.
- **PLAN-CADENCE NOTE (Ariel's q, answered):** trades do NOT require plan updates by design
  (plan=strategy, execution=tactical); today's refresh was needed only because the plan
  EMBEDS derived numbers as literals. The §5b aftermath items ({{fact:key}} + shock fact
  keys) make the plan render numbers live — after that, the plan touches are annual/structural
  only, as intended.

## 5e. FINAL WRAP (2026-07-11 late night — supersedes 5d where they differ)

- **Synthesis-aftermath block REVIEWED + MERGED (master `ea36949`, pushed).** 246 tests green;
  deep review: 3 PASS, 1 CONCERN. **Follow-up mini-block for the aftermath agent:**
  (1) `_preserve_unimplicated_sections` drops MODEL-NEW sections (orchestrator.py:5190-5220) —
  LIVE issue: v89 is 16/18 (healthcare/insurance missing) and can never regain them; union
  model-new sections + dedupe by (section_id,horizon) + tests for duplicates/implicated;
  (2) wrap `_persist_accept_gate_reject` call (plan.py:3573) in try/except (JSON/DB error
  turns the 422 into a 500); (3) accept-gate blob re-feeds one redundant cycle until accept
  (known, converges); (4) pre-existing: plan_narrative_auto_regen worker threads outlive
  tests (hygiene). NOTE: the agent's first 5 commits landed interleaved UNDER the reviewer's
  b04c0cb and reached origin pre-review (second silent-interleave incident — implementers:
  announce pushes).
- **Discovery verdicts (all DEFENDED, registry seeds): OKLO HOLD** (clock: July-2026 first
  criticality — fires THIS MONTH), **RKLB HOLD** (two make-or-break events), **ASTS HOLD**
  (next launch/data-integrity checkpoint), + ORCL HOLD (5d). **Ariel decided: monitor-only
  on all four; the half-starter-before-event option was offered and declined for now.**
- **CMPS: Ariel approved buying.** Proposal 15 was shadow=1 (funnel calibration mode → no
  Approve button in /inbox — the bug he hit). UN-SHADOWED with proposals_history note;
  T2, no TOTP; he clicks Approve; card expires 07-13 15:30. VOR (16) left shadow to expire.
  QUEUE: design a proper funnel-proposal graduation mechanism (shadow → approvable after
  calibration; benchmark 28/29 + 4/5 supports it).
- **ITEM B green-lit for the freed stream-B agent** (prompt in the 2026-07-11 session log /
  re-issue from §3.B + tracking extension): verdicts registry + pushback gate + deterministic
  trigger checker; migrations 0087; zero live LLM; runs PARALLEL to the aftermath follow-up
  mini-block (0086 lane).
- Dirty domain stamps (tax/* + sec_13f.md) — commit as chore next session.

## 5f. LATE ADDENDUM (post-wrap merges)

- **Item B MERGED + LIVE** (6e25c6b after soft-rollout fix; 0087 applied; 8 seeds incl. OKLO
  dated trigger 2026-07-31; trigger cron registered; buy gates flag default-OFF).
  OPEN: tick(now=) signature fix (loop crashes via scheduler — handed back).
- **Items C + D MERGED** (eb2a805 retract-on-reversal atomic w/ verdict write; 66537d0
  discovery_reserve subtracted+labeled in deploy-cash, conservation tested).
- **Aftermath follow-ups MERGED** (8dd635e preserve-merge union/dedupe/implicated-skip;
  2761f15 422 fail-soft; e0e6eb2 **patch/sliced synthesis DEFAULT-ON** (kill switches, conftest
  forces OFF in tests); ecbdf2c Source-6 landed-suppression). Next corrective refresh runs
  PATCHED (~20 min class).
- Remaining §3: E streams D/C/E · G1/G2 (G3 verifies 07-13) · H (needs tokens) · I tail
  (reliability trio prompt ready, {{fact:key}}, test_api_phase4 hang, funnel graduation,
  stream-B non-blocking follow-ups, Fable-5 A/B, sdc/omk re-burns).

## 6. ITEM F BLOCK 2 WORK ORDER — case backlog (fresh implementing agent starts HERE)

You are a NEW external implementing agent closing item F. Branch `feat/opens-2026-07-11`
(HEAD at writing `49cf456`), hand back per block, NEVER merge — the resident Claude Code
session reviews and merges. §0 (read-first list) and §1 (environment discipline) apply to you
in full. Then read: `docs/design/fleet_calibration_benchmark.md` (§1 decontamination protocol,
§2a five-agent pipeline, §2b report format, §3 recorded scoring calls),
`evals/fleet_calibration/PACKET_GUIDE.md` (alias/k registry, protocol checklist),
`evals/fleet_calibration/README.md`, and §5 below (block-1 review verdict — your inherited
follow-ups).

**Block 2a — pipeline follow-ups first (from the block-1 review, §5):**
1. Sanitizer source-manifest decorrelation: stage 2 currently anchors its
   `absolute_figure_rescaling` proof on the CLASSIFIER's `sourced_facts`
   (`agent_pipeline.py::build_sanitizer_input`) — a stage-1→2 correlated-failure channel.
   Give stage 2 an independent verification path (raw-source access or an independently
   constructed manifest).
2. Legacy-resume regime bug: resume stamps `pipeline_version=1` on pre-pipeline unreported
   runs, retroactively disqualifying their OK points at score time. Preserve the legacy
   regime for legacy files.
3. Delete or fix dead `build_classifier_receipt` (emits an agent_role string
   `verify_classifier` rejects — can never pass).
4. `prepare_classifier.py` hard-raises mid-batch on an existing receipt (even in --dry-run);
   skip-and-report instead. Receipts stay write-once.
5. Eval tests aren't collected by the default suite (`testpaths=["tests"]`) — add collection.
6. Add a direct test that stage-3's trader payload excludes `real`/`expected_classes`
   (currently enforced by code shape only).

**Block 2b — the case batch, built THROUGH the five-agent pipeline (stage 1 sources, stage 2
signs off; never hand-build packets):**
- Named cases: AMD-2016 F1, CVNA-2023 re-entry.
- Categories: obscure small-cap failures, trap-shaped winners, winner-shaped failures,
  fresh synthetics.
- **BINDING acceptance (owner emphasis 2026-07-11):** a MAJORITY of new cases must be
  OBSCURE — names with no famous narrative arc (the scored run proved names aren't the only
  fingerprint: scrubbed scar narratives reproduced in 3/29 points). Include synthetics
  (unmemorizable by construction). Prioritize HARD cases — the suite sits at 28/29 and only
  regains discriminating power through winner-shaped failures and trap-shaped winners. The
  stage-4 groundedness score (packet-facts vs imported story knowledge) must be reported
  PER NEW CASE in the §2b table.
- Ownership blindness per §2a is binding (neutral synthetic portfolio for A/B; anonymized
  sleeve context for C/D).

**Cost discipline:** real-LLM scoring points are `llm_eval`-class — coordinate with the
reviewer session BEFORE burning calls; `--dry-run` first, always. Scored runs are immutable
(now enforced in code) — new runs take a NEW `--out` path. Commit per logical block.
