# Argosy — living handover

**This is the ONLY handover file.** It is a living document: update it in place, don't add dated siblings. The 33 dated handovers that used to live here (2026-06-01 → 2026-08-12) were consolidated into this file on 2026-08-12 and deleted; they remain in git history at `87ca7f3` — `git show 87ca7f3:docs/handovers/<name>.md` to read one, `git log --diff-filter=D --name-only -- docs/handovers/` to list them.

Last updated: **2026-08-12**.

---

## North star (why any of this matters)

Argosy is a trustworthy, always-on financial brain for one family. It holds the whole picture (holdings, RSUs, pensions, cashflow, Israeli tax, FX, life events), forms its own opinions, and is right, current, and self-consistent across `/plan`, `/portfolio`, `/retirement`. **The user should not have to be the investing expert — Argosy is, out loud.** Operationally: maximize finances + earliest safe retirement, with Argosy as the back office — Ariel is in the loop only when something genuinely needs him.

Canonical copy: `docs/design/SDD.md` → `## North star`. Auto-memory: `project_argosy_north_star`.

---

## State (authoritative — verified 2026-08-12)

| Thing | Value |
|---|---|
| master | `eccade9` = **origin/master — pushed, in sync** |
| Push | blocked by the harness classifier → Ariel runs `! git push origin master` himself |
| Migrations | `0101_fill_verdict_link` (DB confirmed at 0101) |
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

## OPEN QUEUE (priority)

### 1. ⚠️ Regenerate the plan — the highest-value open item
The current plan predates the restore, and the draft was produced by a fleet whose concentration input excludes the unmanaged NVDA. Two hard preconditions, then a codex-free regen:

**Precondition A — fix the analyst-input plumbing. STILL UNFIXED on master (verified 2026-08-12).**
`1f6ca68` fixed the *resolver* (the final rendered number) but not the analyst's input. `argosy/orchestrator/flows/plan_synthesis/inputs.py::_summarize_positions` (defined :1287, comment at :1310) still keeps the summary "focused on tradeable holdings", so the **unmanaged Schwab NVDA is excluded from the ConcentrationAnalyst's input**. (The often-quoted "run 284 logged *no NVDA position … weight 0.0 UNKNOWN*" could not be reproduced from current logs — the code-level exclusion is the verified part; treat the log quote as hearsay.) Until this surfaces unmanaged-but-present NVDA, a fresh synthesis reasons on a book missing its largest position. **Fix before firing any regen.**
→ Branch `worktree-agent-afb7cdd941018a1fc` ("count present-but-unmanaged NVDA toward concentration %") looks like exactly this fix, sitting unmerged. Check it first.

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
