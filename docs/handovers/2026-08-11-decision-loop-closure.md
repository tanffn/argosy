# Handover — decision-loop closure + backend live (2026-08-11, evening)

## Fresh-session reading order
1. This file (current state + open items + the RESTART caveat).
2. `git log --oneline -12`.
3. `docs/handovers/2026-08-11-verdict-authority-and-book-goal.md` — the book-trust goal (CLOSED) + spine/one-voice/verdict-authority detail.
4. Memory `feedback_one_voice_verdict_vs_plan`.

## Git state (authoritative)
- **master = `e97e56b`**, **ahead of origin by 3 (a9d844f, f0a02d3, e97e56b) — UNPUSHED** (+ this handover commit). `git push` blocked by the harness classifier → **Ariel runs `! git push origin master`**.
- Migrations head **0100** (no migration added in any of this session's loop/positions/authority work).

## ⚠️ RESTART CAVEAT — running backend is BEHIND the repo
The supervised backend was restarted at **19:03 today** (PID chain via `scripts/start_backend_detached.ps1`; supervisor scheduled task "Argosy Backend Supervisor"). It is LIVE and healthy (/health, /api/portfolio/snapshot, /api/positions/thesis all 200) and runs the one-voice + verdict-authority + spine code. **BUT three commits landed AFTER that restart and are NOT live yet:** `a9d844f` (/positions reasoning), `f0a02d3` (loop plumbing), `e97e56b` (greeting surfacing). **A FINAL backend restart is required to make them live** (kill the 4-process chain, then `scripts/start_backend_detached.ps1 -Port 8000` with `ARGOSY_EXPENSE_SAMPLES_ROOT` set — see the restart recipe below).

## What shipped this session (post-goal)
- **Backend restarted** onto today's code (was an Aug-9 process running pre-fix code).
- **`a9d844f` /positions fix** — cards showed "no data" (~37/39, e.g. CSPX) because reasoning was sourced from the empty draft-plan layer (v94/run 284 has 0 agent_reports). Now `project_thesis_dtos` surfaces the settled VERDICT's reasoning (via `provenance_for_subjects`, which now carries `reasoning_md`), prepending stance notes. Pre-existing bug since 2026-07-10, not this session's doing.
- **`f0a02d3` decision-loop plumbing (seams 1+2, Sol PASS)** — (1) every settled verdict now gets `next_validation` (explicit → soonest strictly-future dated_event → today+45d); (2) each actionable verdict emits ONE graded prediction keyed by `verdict_id` (source `signal_stream:deep_decision_verdict`, dedup by message_id), so the ALREADY-LIVE daily evaluator scores the actual verdicts. No feedback loop (verified — pseudo-source not consumed as a decision input). No migration.
- **`e97e56b` loop surfacing (step 3)** — a "how_our_calls_did" home-greeting section shows past verdicts' wins/misses in plain language ("SELL NVDA (Aug 8): NVDA -6% — good call" / "…-8% — miss, worth revisiting"), honest raw-move, best-effort. DTO field added; **UI render deferred** (needs a card block in `ui/src/components/home/FMGreetingCard.tsx` + a type in `ui/src/lib/api.ts`).

## The decision-learning loop — status after this session
User's loop = record verdict+reason+recheck → user acts → revisit on date → learn from outcome → surface win/mistake.
- **Record + recheck date:** ✅ now (next_validation on all, + typed triggers + the deterministic sweep that auto-fires a re-decision).
- **Learn (grade the verdict):** ✅ now — verdict→prediction bridge + the live evaluator grade price-after-decision keyed by verdict_id.
- **Surface:** ✅ backend (greeting section); UI render pending.
- **Did the user act? (fill→verdict link):** ❌ STILL OPEN = **seam 4** (task #37). Fills link to proposals, not verdicts; add `verdict_id` at fill-write in `argosy/execution/reconcile.py`. Low urgency (0 fills today) but it's the last structural gap. MONEY PATH — careful + Sol review.

## OPEN QUEUE (priority)
1. **FINAL BACKEND RESTART** so a9d844f/f0a02d3/e97e56b go live (and /positions shows reasoning). Recipe below.
2. **#35** (task) — falsifier evidence-contract + SELL falsifier-vs-accelerant (both batch-2 reviewers named it): trader-prompt improvement so every falsifier carries source/as-of/baseline/threshold/persistence/decision-transition; for a SELL, separate true falsifiers (concentration back in band → STOP selling) from downside accelerants; ETF falsifiers need numeric thresholds; fix NVDA "FY26 Q2"→"Q2 FY2027"; qualify SCHD "don't sell" re: the SCHD→FUSA estate migration. Primarily `argosy/agents/trader.py` prompt (LLM-team lever, not a gate).
3. **Seam 4** — fill→verdict_id linkage (`execution/reconcile.py`), money path.
4. **UI render** for `how_our_calls_did` (FMGreetingCard.tsx + api.ts).
5. **#24 Phase 3c enforcement** — deferred (behavior-changing).
6. Re-run the lean Sol confirms self-verified during codex outages (3d round-4 delta; verdict-authority round-2) when codex is stable.

## Backend restart recipe (Windows PowerShell)
```
# 1. list: Get-NetTCPConnection -State Listen -LocalPort 8000; the 4-proc chain = run_backend_service.py x2 + uvicorn x2
# 2. Stop-Process -Id <each of the 4> -Force   (kill top supervisor first; children die with it)
# 3. verify port 8000 free
# 4. $env:ARGOSY_EXPENSE_SAMPLES_ROOT = "D:\Google Drive\Family\Finances\Portfolio\Resources"
#    & "D:\Projects\financial-advisor\scripts\start_backend_detached.ps1" -Port 8000
# 5. health: Invoke-WebRequest http://127.0.0.1:8000/api/health   (expect 200)
```
The `start_backend_detached.ps1` idempotency guard refuses to start if a supervisor+busy-port already exist — so you MUST kill the old stack first.

## Traps / discipline
- **Codex/Sol flaky** — model gpt-5.5, LEAN prompts finish / heavy get killed. Kit: `tmp_review/_run_codex_review_long.py`. Each money/decision unit: build → Sol → fix → commit. UI/read-projection: skip Sol per CLAUDE.md.
- **NEVER write live `db/argosy.db`.** Fleet-run validation uses `scratchpad/run_fleet_resumable.py` against a hash-guarded COPY via `init_engine(<copy-url>)` (ARGOSY_DB_FILE env does NOT override; db_file is default_factory'd). The session KILLS long background commands (~1 consult) — the resumable per-ticker harness survives kills; re-invoke to resume.
- Stage commits by explicit path (never `git add -A`); `.tmp_*`/old handovers/`domain_knowledge/*.md` are pre-existing noise. Don't pop `stash@{0}`.
- PYTHONIOENCODING=utf-8; ASCII `-`/`+` in greeting output (cp1252 console).
