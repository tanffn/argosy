# Argosy — living handover

**This is the ONLY handover file.** It is a living document: update it in place, don't add dated siblings. The 33 dated handovers that used to live here (2026-06-01 → 2026-08-12) were consolidated into this file on 2026-08-12 and deleted; they remain in git history at `87ca7f3` — `git show 87ca7f3:docs/handovers/<name>.md` to read one, `git log --diff-filter=D --name-only -- docs/handovers/` to list them.

Last updated: **2026-08-15**.

> **All 2026-08-14/15 work is MERGED, PUSHED and DEPLOYED** — master `86a50df`, backend live on it, migrations 0102+0103 applied. No branch in flight.

> **⚠️ THE HEADLINE FINDING (2026-08-15).** The plan reported FI "REACHED" with a +616,678 NIS cushion. That was GROSS of realization tax. Ariel's own per-lot RSU simulation (`tax_simulation_lots`, 57 rows, all 10,940 NVDA shares) was being read for ONE key (`concentration.nvda_eligible_now_sh` = 9,230) and never for the tax. Derived and now published:
>
> | | NIS |
> |---|---|
> | FI margin GROSS | **+616,678** |
> | NVDA embedded realization tax (revalued @ $225.16) | **2,510,030** |
> | **FI margin NET OF REALIZATION** | **−1,893,351** |
>
> The tax is **4.1× the entire gross cushion**. Six render sites now route through one shared helper so no surface can print "FI reached" without stating the basis. The FM had reported "no Schwab tax lots loaded" because it checks the `lots` table (0 rows) while the data is in `tax_simulation_lots`. Execution tracker with per-task checkboxes: `docs/superpowers/plans/2026-08-13-trust-restoration-execution.md`. Design: `docs/superpowers/specs/2026-08-12-trust-restoration-design.md`. Audit evidence: `docs/superpowers/INDEX.md`. Read the tracker before picking anything up — it records what landed, what was disproved, and what awaits Ariel.

---

## North star (why any of this matters)

Argosy is a trustworthy, always-on financial brain for one family. It holds the whole picture (holdings, RSUs, pensions, cashflow, Israeli tax, FX, life events), forms its own opinions, and is right, current, and self-consistent across `/plan`, `/portfolio`, `/retirement`. **The user should not have to be the investing expert — Argosy is, out loud.** Operationally: maximize finances + earliest safe retirement, with Argosy as the back office — Ariel is in the loop only when something genuinely needs him.

Canonical copy: `docs/design/SDD.md` → `## North star`. Auto-memory: `project_argosy_north_star`.

---

## State (authoritative — verified 2026-08-12)

| Thing | Value |
|---|---|
| master | **`94f02d5`** = origin/master — trust-restoration merged, pushed, deployed |
| Working branch | `feat/objection-loop` — closing the FM objection dead-end |
| Migrations | `0103_instrument_classification` (0102 gate_outcomes, 0103 sector caps, both applied) |
| Push | blocked by the harness classifier → Ariel runs `! git push origin master` himself |
| Working tree | clean; **no worktrees** |
| Backend | up on :8000, healthy (`/api/health`, `/api/portfolio/snapshot`, `/api/positions/thesis` all 200) |
| UI | up on :1337 (Next.js 16.2.4 / Turbopack) |
| Current plan | `refinement-2026-07-13-165608` — **pre-restore, from 2026-07-13** |
| Newest draft | `synth-2026-08-08-2200` — **do NOT accept** (see NVDA note) |
| Backup | `scripts/backup_to_sibling.ps1` → `D:\Projects\financial-advisor-backup` |

**Book (post-restore truth):** a 2026-07-13 Leumi-only-import bug erased $2.4M / 59% of the book (incl. NVDA); it was restored on production 2026-08-08/09. US-situs estate ~$1.28M.

**⚠️ NVDA — three live numbers, verified 2026-08-12.** `/api/portfolio/snapshot` returns `Strategic single-stock (NVDA): pct 58.02, target_pct 8.0`, while the **IPS prose says 12%** (a RED cross-surface finding logged 2026-07-07 and still open). Book **58.02%** / plan target **8.0%** / IPS **12%**.
Earlier handovers said the draft was "computed with NVDA @ 0%" — **that phrasing was wrong and is retired.** Nobody set a target of 0%. What exists: ConcentrationAnalyst report 2915 *recommends* `"force cap to 0% and liquidate to target"` under a zero-FI-delay-tolerance branch, and the analyst's input excludes the unmanaged Schwab NVDA (precondition A below). The claimed log line "current NVDA weight 0.0 UNKNOWN" could **not** be found in current logs — treat it as unverified.

---

## Trust-restoration pass — state as of 2026-08-13

**Landed on `feat/trust-restoration`** (tests green at each step; Sol reviewed the money paths):
- `9dba3cf` `GateOutcome` contract — tri-state PASS / BLOCK / **DID_NOT_RUN**, where DID_NOT_RUN blocks promotion. A non-PASS must carry a reason; an override must name who and why. Silent overrides are unrepresentable.
- `bcd9179` `/overview` shipped — was fully built and had no page route. 200, consistency guard 5/5 (first live exercise).
- `a10edc1` **fail-closed everywhere**: reader `None` blocks; codex math gate reports an outcome on every path; `/accept` graph-build exception no longer falls through to an open-flag-free promotion; `/refine` refuses to stage a draft whose invariants never ran (422); digest fails when it sends nothing. Plus **1B**: plan burn now derived from real transactions.
- `1fc2b75` SDD:1354 corrected — it had claimed the reader was fail-closed while the code did the opposite.
- `80d226d` deconcentration-optimizer route + VAPID generator.

**Ariel's rulings (2026-08-13):** thin-month threshold **50**; planning burn **rounds UP to the nearest ₪1,000** → **₪25,000** from a derived ₪24,032 (11 months, 1,082 txns). Implemented as a *labelled buffer on a derived number* (`monthly_burn_raw_nis` + `monthly_burn_buffer` travel alongside), never a typed figure.

**⚠️ Two claims this pass DISPROVED — do not re-fix:**
1. **Precondition A was never broken.** Executing `_summarize_positions` on the live snapshot yields `NVDA qty=10940 value=$2,379.4k acct=schwab` — the true 57.7%, already in the analyst's input. Four handovers propagated a misread docstring; nobody ran it. Cherry-picking `70008d4` was a no-op.
2. **Discord was not a silent failure.** `config.py:209` records a deliberate shutdown on 2026-07-07 (reconnect bug ~150 supervisor restarts/day, Discord blocked the API, **0 signals since 2026-05-29**). Auth-4004 errors date from 2026-06-15. Re-enabling was gated on a *value review that never happened*. **Ariel said "we need to fix the feed" — that decision is open**, and should start from whether a source with 0 signals in its final 5 weeks and a 21% hit-rate earns the work.

**Open money-math decisions (deliberately NOT applied):**
- **CGT taper binding.** `scenario_mc.py:552` pins `DECONCENTRATION_TAPER_YEARS = 3`; the plan headline never consults the optimizer's chosen horizon. SDD §19.4 is partially false (shared formula, unshared horizon). Worth ~₪100–170k and up to a year of retirement age.
- **The optimizer currently sees no NVDA at all** — `nvda_current_pct=None`, `sell_nis=0.0` for every horizon, because plan v92 has `decision_run_id=None` (refinement-path draft) so a `if drun is not None` guard skips resolution and a bare `except: return 0.0, None, None` hides it. **Expected to resolve itself once the regen creates a decision run — verify, don't assume.**
- **A FOURTH NVDA number:** `DEFAULT_NVDA_CAP_PCT = 0.13` (`scenario_mc.py:553`), alongside book 58% / plan target 8% / IPS prose 12%.

**Needs Ariel (blocked on a human):**
- SMTP creds. Set then restart: `ARGOSY_SMTP_HOST` (e.g. `smtp.gmail.com`), `ARGOSY_SMTP_PORT=587`, `ARGOSY_SMTP_FROM`, `ARGOSY_SMTP_USERNAME`, `ARGOSY_SMTP_PASSWORD` (Gmail app password). Send path already verified end-to-end against a local `aiosmtpd` sink.
- **Web push final step:** open `http://localhost:1337/settings/notifications`, click enable, grant the Chrome prompt. `notification_subscriptions` is 0 until then; VAPID keys now exist and the endpoint returns 200.
- **73 open action proposals** (not 59 — the queue grew), **40 stale >30d**: 45 `note_only`, 14 `set_watchlist`, 6 `update_plan_assumption`, 4 `rebalance` (all stale, likely superseded by the regen), 1 `replan_full` (2026-07-26, actionable — it is a regen trigger), 1 each `stock_decision` / `deploy_team_flag` / `allocate`. Nothing deleted.

**Trust-restoration is DONE and deployed.** Sol reviewed every money path across three passes: 11 blockers found and fixed, final verdict COMMIT AS-IS. Two of those blockers were introduced BY the fix work and were the exact pattern the branch removes — a swallowed burn exception relabelled "insufficient data", and a non-finite guard that raised into a handler which wrote the bad value through anyway.

**Burn corrected again:** refunds were never netted (the query filtered `direction=="debit"`). Real burn is **₪22,519 raw → ₪23,000 planning**, not the ₪25,000 fed to regen run 359. `/dashboard-overview` does NOT share the bug.

**⚠️ The 12-vs-13 NVDA cap is now DETECTED, not silent.** The coherence gate fires on the live artifact: `nvda_cap_pct disagrees across surfaces (body=12.0; alloc_doc=13.0; prose=13.0)`. The RESOLVER carries 12; the allocation doc and prose carry 13. Per CLAUDE.md this is the derivation-question class — **zigzag it before the next regen, or synthesis will block on it.**

**OPEN — the FM objection loop (workstream 2, in progress on `feat/objection-loop`).** Run 359: 7 FM objections, **0 dispatched**, all skipped at log level INFO, user told nothing. Causes: owner routing is a regex over prose (`fm_objection_dialogue._parse_analyst_refs_any_form`); `WithdrawalSequencerAgent` and `FXAnalystAgent` are absent from `ANALYST_AGENT_NAME_TO_ROLE`; `ARGOSY_FM_DIALOGUE_CONVERGE` defaults `"0"`. The machinery to fix it already exists and is simply unwired — `negotiation_ladder.run_ladder` (peer rounds → arbiter → `ESCALATED_TO_USER` with a question) and `critique_reconcile`'s `needs_user_input` → open ActionProposal.

---

## OPEN QUEUE (priority)

### 1. ⚠️ Regenerate the plan — the highest-value open item
The current plan predates the restore, and the draft was produced by a fleet whose concentration input excludes the unmanaged NVDA. Two hard preconditions, then a codex-free regen:

**Precondition A — ✅ ALREADY FIXED. Empirically disproved 2026-08-13.**
Running `_summarize_positions` against the live snapshot yields `NVDA qty=10940 value=$2,379.4k USD @ $217.50 acct=schwab` — i.e. **57.7% of the $4,121.4k book, present in the ConcentrationAnalyst's input.** The resolver half (`plan_numeric_resolver._apply_nvda_current_weight`) also already counts present-but-unmanaged NVDA on master; cherry-picking branch `70008d4` produced a **no-op** (3 comment lines).
The claim below was inherited through four handovers and repeated without ever being run. The docstring phrase "stays focused on tradeable holdings" refers to skipping **symbol-less** rows (cash sentinels, real-estate, pension) — *not* to excluding unmanaged positions. **Do not re-fix this.** Retained for the record:

<details><summary>superseded claim</summary>
`1f6ca68` fixed the *resolver* (the final rendered number) but not the analyst's input. `argosy/orchestrator/flows/plan_synthesis/inputs.py::_summarize_positions` (defined :1287, comment at :1310) still keeps the summary "focused on tradeable holdings", so the **unmanaged Schwab NVDA is excluded from the ConcentrationAnalyst's input**. (The often-quoted "run 284 logged *no NVDA position … weight 0.0 UNKNOWN*" could not be reproduced from current logs — the code-level exclusion is the verified part; treat the log quote as hearsay.) Until this surfaces unmanaged-but-present NVDA, a fresh synthesis reasons on a book missing its largest position. **Fix before firing any regen.**
→ Branch `worktree-agent-afb7cdd941018a1fc` is **already effectively on master** — safe to delete in branch triage.
</details>

**So the only remaining blocker to the regen is Precondition B** (below): the math gate must fail CLOSED.

**Precondition B — replace the codex math-gate with an in-harness blind re-derivation.**
`plan_synthesis/codex_second_opinion.py` is the blind headline-number audit that BLOCKs a plan whose NVDA weight / estate / net worth don't independently re-derive. When codex was 401-dead it returned `(None,None)` fail-soft — which is exactly how the bad v94 draft got green-lit. Codex is alive again (model `gpt-5.5`), but plan for it dying: substitute a read-only in-harness agent that re-derives NVDA weight, US-situs estate and net worth from raw snapshot rows and BLOCKs on divergence from the known-true numbers above.

**Regen sequence:** fix A → restart backend on the new SHA → fire `POST /api/advisor/check-in` → run the math audit (B) → present the draft to Ariel, **do NOT auto-accept**. Also re-run the sequence-aware retirement MC (`canonical_feasible_dual_track`) on the true book.

### 2. ⚠️ Make the guardrails fail CLOSED — the audit's central finding
Three mechanisms **report success when they did not run** (full evidence in `docs/superpowers/INDEX.md`):
- **Codex math gate** — `codex_second_opinion.py` returns `(None,None)` fail-soft; this is how the bad draft was green-lit.
- **Whole-artifact coherence reader** — `orchestrator.py:2657` computes `_reader_ok = (_reader_verdict is None or ... != "BLOCK")`, and `whole_artifact_reader.py:643` returns `None` on timeout / missing kit / pytest. **`None` ⇒ approved.** `SDD.md:1354` claims the exact opposite ("fail-closed … never a soft pass") — the SDD is wrong and must be corrected either way.
- **Weekly digest** — `job_runs.status='ok'` while `send_status: skipped`, `send_error: smtp_not_configured`.

This is one design habit, not three bugs: **absence of a result is treated as a passing result.** It is fully consistent with the "determinism is the inviolable-arithmetic floor" rule — a gate that blocks when it *cannot verify* is not judging whether a decision is good.

### 3. Consistency + last-mile gaps found by the 2026-08-12 audit
Ranked; all evidence in `docs/superpowers/INDEX.md`.
- **NVDA reads 58% / 8% / 12%** across book, plan target, and IPS prose. Open since 2026-07-07.
- **Plan never sees real spend** — `inputs.py:1006` feeds the analyst `identity_yaml.monthly_expenses_total_nis` (typed at onboarding); zero `ExpenseTransaction` references in `plan_synthesis/`. SDD §6:544 claims otherwise. Cal/Amex/Diners parsers are `NotImplementedError`.
- **Nothing can reach Ariel** — email skipped (`smtp_not_configured`), `notification_subscriptions` = 0, Discord dead since 2026-07-08 while its 434 stale predictions still appear live in `source_reliability`. 59 open proposals undelivered.
- **Caps are prose-only** — `risk_preflight.py:180` has no sector-cap logic; `PlanPolicy` / `instrument_classification` NOT_BUILT. The 35% tech and 15% single-name caps are unenforced.
- **`/overview` is built and unreachable** — router registered (`main.py:129`), assembler + 10 components exist, but no `ui/src/app/overview/page.tsx` and not in nav.
- **`can_publish_plan` bypassed** on any exception (`plan.py:3839-3847` falls back to bare `evaluate_promotion`).
- **`/refine` money-safety net inert** — `plan.py:5688` omits `post_doc`, so `evaluate_plan_invariants` never runs.
- **Insurance entirely NOT_BUILT** — substitute is a 10×-income heuristic with no policy data.

### 4. #24 Phase 3c enforcement — deferred, needs a decision
The spine gate shipped **warn-first and DORMANT** (`f45acf1`). Flipping money surfaces to route through `read_validated_snapshot` and refuse a non-validated book is behavior-changing; it needs a deliberate call, not a drive-by.

### 5. Re-run the lean Sol confirms
Self-verified during the codex outage: 3d round-4 delta, verdict-authority round-2. Codex is stable again (`gpt-5.5`).

### 6. Triage 10 unmerged branches (no worktree)
| Branch | Unmerged | Tip subject |
|---|---|---|
| `feat/stream-a-data-integrity` | 3 | restore provenance liveness path; keep vintage enforce off |
| `feat/stream-b-bear-independence` | 3 | make independence semantic; choke trader prompt assembly |
| `feat/stream-c-prediction-ledger` | 1 | wip: make the prediction ledger actually score |
| `feat/stream-e-async-cache-loop` | 1 | wip: loop-safe bridge instead of `asyncio.run` |
| `worktree-agent-a10db172e619def77` | 1 | graduate mark-staleness so normal gaps degrade gracefully |
| `worktree-agent-a65837e8271f39123` | 1 | route six raw-TSV readers through one guarded book accessor |
| `worktree-agent-a7ad1ac05ea0314da` | 1 | make silent-success / fail-open job failures visible |
| `worktree-agent-a8c47c9e4b388501d` | 1 | label opaque symbol-less rows; stop live-DB test |
| `worktree-agent-aab67d6154b20533a` | 1 | stall alert must alert-not-throw + `busy_timeout` on sync engines |
| `worktree-agent-afb7cdd941018a1fc` | 1 | count present-but-unmanaged NVDA toward concentration % ← **see item 1** |

### 7. Smaller open items
- **6 decision-funnel tests red on master** (`test_decision_funnel_position_context`, `_sleeve_mandate`) — they hit the live DB and short-circuit because SOFI has a settled verdict. Live-DB test-isolation defect (same class as `9b98085` fixed), **not a source bug**. Isolate with a fixture DB.
- **~40 sync `create_engine(` sites** lack `busy_timeout` — migrate to `create_sync_engine()`. Mechanical; `8963406` did the two hot ones.
- **Owner action:** a fresh Schwab statement would close the restore's carry-forward assertion (it assumes nothing traded in Schwab / Schwab 876 / Aborad since 2026-07-13 — Ariel confirmed, but it's unverifiable from inside the system).

---

## Recently shipped (don't re-do)

Prior handovers listed these as open **after** they had already shipped — check `git log` before believing any open-item list, including this one.

- **`b2e331c`** UI render of the "how our calls did" greeting scorecard.
- **`f45acf1`** spine Phase 3c, warn-first + DORMANT.
- **`0c4d692`** seam 4 — executed fills link back to the verdict that recommended them (+ migration 0101).
- **`fbf2030`** #35 falsifier evidence-contract + concise advisor-voice verdicts.
- **`a9d844f` / `f0a02d3` / `e97e56b`** the decision-learning loop: `/positions` surfaces settled-verdict reasoning; every verdict gets a recheck date and becomes a graded prediction keyed by `verdict_id` so the live evaluator scores it; greeting surfaces wins/misses.
- **One-voice fix (Phases 1–3, Sol-passed)** — resolved a verdict-vs-stance split-brain (NVDA had a settled HOLD contradicting the plan's standing SELL). Binding ruling in memory `feedback_one_voice_verdict_vs_plan`: a per-holding verdict contradicting the plan's standing stance is a **bug — fix the inputs, not with a gate**.
- **Book-trust goal CLOSED** — both reviewers GOOD.
- **P1 restore complete** — the $2.4M erasure was restored on production and the fallout fixed across 7 commits.

---

## Working discipline

- **The LLM TEAM is the architecture — not deterministic gates.** A *judgment* failure (bad rationale, wrong instrument, imprudent call) is caught by ANOTHER agent re-deriving blind — never by adding a per-symptom deterministic gate (whack-a-mole antipattern). Determinism is the **inviolable-arithmetic floor only** (conservation, estate / US-situs). Default response to a bad decision: *"which agent should have caught this, and why didn't it — fix the team."*
- **Accuracy over LLM cost.** Synthesizer + bull/bear/trader/fund_manager/audit/plan_synthesizer default to Opus. No Haiku defaults.
- **Ask, don't assume.** Surface judgment calls to Ariel.
- **Escalation bar: fatal FORKS only.** Two judges disagreeing on a value/wording is a DERIVATION question — zigzag it (each side argues from raw sources, a blind third re-derives, converge, record the rationale). Ariel gets only structurally different PATHS (sell-vs-hold the core, adopt-vs-exit a class, goal changes).
- **Money/decision units:** build → Sol → fix → commit. Skip Sol for UI / read-projections.
- **Manual UI smokes are skipped** — backend tests + live-LLM e2e are the verification surface.

## Traps

- **Codex/Sol:** model `gpt-5.5` (a ChatGPT-account codex rejects literal `--model sol`). LEAN prompts finish; heavy ones get killed.
- **Never junction-link `<worktree>/ui/node_modules`** to main's — it has twice wiped main's `@babel/` scope on `git worktree remove`. Use `cd <worktree>/ui ; npm ci` (~45 s).
- **`git worktree remove` does not delete commits** — branch refs survive. Safe as long as you don't also `git branch -D`.
- **Console is cp1252** — set `PYTHONIOENCODING=utf-8` for anything printing ₪ / → / Hebrew, and do durable side-effects BEFORE printing (a print crash silently killed three domain-refresh runs).
- **Windows 32,767-char argv cap** — this was the real cause of "claude.exe not found". Pass system prompts >8k via `--system-prompt-file`, never inline (`3b9ae78`).
- **`proposals.account_class`** — the DB string is `"limited"`, never `"argonaut"`; the broker router checks that exact value.
- **Don't bypass** `argosy/services/file_catalog.py::catalog_upload` for new user-byte-blob ingest paths (SDD §17.1).
- **Only `docs/design/SDD.md`** is canonical — never edit `.docx` siblings.

## Restart recipes

**Backend** — kill the 4-proc chain first; the start script's idempotency guard refuses if a supervisor + busy port already exist:
```powershell
Get-NetTCPConnection -State Listen -LocalPort 8000        # find the chain
Stop-Process -Id <top supervisor> -Force                  # children die with it; verify all 4 gone
$env:ARGOSY_EXPENSE_SAMPLES_ROOT = "D:\Google Drive\Family\Finances\Portfolio\Resources"
& "D:\Projects\financial-advisor\scripts\start_backend_detached.ps1" -Port 8000
Invoke-WebRequest http://127.0.0.1:8000/api/health        # expect 200
```

**UI** — must be launched detached; a double-background from bash silently loses the process:
```powershell
Start-Process cmd.exe -ArgumentList "/c","npm run dev" -WorkingDirectory "D:\Projects\financial-advisor\ui" `
  -RedirectStandardOutput D:\Projects\financial-advisor\tmp\ui_dev.log `
  -RedirectStandardError  D:\Projects\financial-advisor\tmp\ui_dev.err.log -WindowStyle Hidden
```

**Tests:** `.venv/Scripts/python.exe -m pytest -m "not llm_eval" <path>`
