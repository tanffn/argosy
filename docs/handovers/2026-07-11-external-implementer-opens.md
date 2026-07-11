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
hand-backs. HEAD at writing: `0ba86c2`. You do NOT execute items A-I yourself unless the
implementer stalls and Ariel redirects — your work:

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

- **Stream-A post-merge review** (Claude Code side): code review + live-cycle verify-run on
  master — its verdict may add fix items to §3.E; wait for it before signal-stream work.
- Hourly domain-refresh job re-stamps `domain_knowledge/*` — commit those stamps as their own
  chore commit if they land in your tree; never mix them into feature commits.
- The reviewer runs `verify-run` on any live cycle you produce and the calibration benchmark
  on any trader/mandate prompt change you make (SDD §14.6/§14.8 — eval-first is mandatory).
