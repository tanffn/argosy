# Argosy — fresh-agent router

Multi-agent financial-advisor system. Python + FastAPI + Next.js. Single user today (Ariel + Noga); multi-tenant ready by design. This file exists to keep a fresh LLM from burning tokens skimming a ~70k-token SDD before doing useful work.

## Read these first (in order)

1. **`docs/design/SDD.md`** — the canonical design doc. It is a USER-AGNOSTIC technical document (mechanisms + where user state lives; current state only — no history, no handover sections, no tenant plan values as facts). The section to read for code investigation: `## Quickstart for new agents — where to find things` — task → file router, conventions/gotchas list, filesystem layout. Session state lives in `docs/handovers/` (see the fresh-session pointer below), never in the SDD.
2. **`graphify-out/GRAPH_REPORT.md`** — 30+ named architectural areas with their member files. Complements the SDD task→file table from the file→area direction. Run `graphify query "<question>"` for cross-file traces (~26× token reduction vs reading raw files).
3. **`~/.claude/projects/D--Projects-financial-advisor/memory/MEMORY.md`** (Claude Code only) — binding user preferences. The ones below are duplicated here so non-Claude agents see them too.

## Binding preferences (verbatim)

- **Accuracy over LLM cost.** Prefer thorough/accurate options over cheaper ones. Synthesizer + bull/bear/trader/fund_manager/audit/plan_synthesizer default to Opus; no Haiku defaults remain (see SDD §3.8).
- **Ask, don't assume.** Surface judgment calls to Ariel; don't auto-decide like prior LLM waves did.
- **`docs/design/SDD.md` is the only canonical SDD.** Do NOT edit `.docx` siblings or any other "SDD" file. If a tool offers to update both, decline.
- **Manual UI smokes are skipped.** Backend tests + live-LLM e2e are the verification surface; don't propose manual click-through plans.
- **Use the codex-tandem kit for risky work** (money math, parsers, migrations, decision flows). Skip for UI / lint / docs. Kit at `tools/codex-tandem/` (gitignored, NVIDIA-internal). See `reference_codex_tandem.md` in auto-memory for invocation patterns.
- **Escalation bar: fatal FORKS only (2026-07-09).** Two internal judges disagreeing on a value/wording (the 12%-vs-13% cap class) is a DERIVATION question — zigzag it (each side argues from raw sources, a blind third re-derives, converge, record the rationale), never escalate. Ariel gets only structurally different PATHS (sell-vs-hold the core, adopt-vs-exit a class, goal changes) where defensible derivations irreconcilably diverge — then decide together. Applies to every needs_user_input route.
- **The LLM TEAM is the architecture — not deterministic gates.** A *judgment* failure (bad rationale, wrong instrument, imprudent call) is caught by ANOTHER agent that re-derives blind — never by adding a per-symptom deterministic gate (that's the whack-a-mole antipattern). Determinism is the **inviolable-arithmetic floor only** (conservation, estate/us-situs); it never judges "is this a good decision." Default response to a bad decision = *"which agent should have caught this, and why didn't it — fix the team (inputs / blind-review / reliability)."* Adding a gate for a judgment failure is the exception and needs explicit justification to Ariel. The team was once cut for timing out — the fix is the P0 reliability wrapper, NOT retreating to determinism. See memory `feedback_fleet_authors_determinism_verifies` (2026-07-04 correction).

## Environment

- **Working dir:** `D:\Projects\financial-advisor\` (= `ARGOSY_HOME`).
- **Python venv:** `.venv/`. Interpreter: `D:/Projects/financial-advisor/.venv/Scripts/python.exe`. Use `uv sync` / `uv run`.
- **Shell:** PowerShell on Windows. `&&` doesn't chain — use `;`, or the Bash tool for POSIX scripts.
- **Console is cp1252** — any script printing agent/plan output (₪, →, Hebrew) MUST set `PYTHONIOENCODING=utf-8`, and do durable side-effects BEFORE printing (a print crash silently killed three domain-refresh runs on 2026-07-08).
- **Dev DB:** `db/argosy.db` (6 sources, ~2,180 transactions ingested).
- **Tests:** `.venv/Scripts/python.exe -m pytest -m "not llm_eval" <path>`
- **UI:** `cd ui ; npm run lint ; npm run typecheck ; npm run dev` (port 1337). Subdir guidance: `ui/AGENTS.md`.
- **Backend:** `uvicorn argosy.api.main:create_app --factory --host 127.0.0.1 --port 8000`. Set `ARGOSY_EXPENSE_SAMPLES_ROOT="D:/Google Drive/Family/Finances/Portfolio/Resources"` before running for RSU reconciliation to find Schwab CSVs.

## Don't do these

- Don't edit `.docx` SDD siblings — only `docs/design/SDD.md`.
- Don't commit `tools/codex-tandem/` contents — gitignored on purpose.
- Don't bypass `argosy/services/file_catalog.py::catalog_upload` for any new user-byte-blob ingest path (SDD §17.1).
- Don't write `"argonaut"` to `proposals.account_class` — the DB string is `"limited"`; the broker router checks for that exact value.
- Don't `--no-verify` past failing hooks or skip signing without explicit ask.
- **Don't junction-link `<worktree>/ui/node_modules` to main's `ui/node_modules`.** Observed twice (Wave B-UI follow-ups #1 and #2) that `git worktree remove` traverses the junction and wipes the `@babel/` scope + `.bin/` from the MAIN repo, breaking the dev server with `Module not found: '@babel/runtime/helpers/interopRequireDefault'`. Right convention: `cd <worktree>/ui ; npm ci` once (~45 s) so the worktree has its own isolated copy. See SDD §"Project-wide conventions / gotchas" for the full incident note.

## Current state — point a fresh LLM here

Don't trust dated prose for "where we are." Use:
- `git log --oneline -20` — last 20 commits, authoritative for recent activity.
- `docs/design/SDD.md` §0, §13, §18 — wave roadmap, refreshed regularly.
- This `CLAUDE.md` only routes; it doesn't track active work.

**Fresh session: start at `docs/handovers/HANDOVER.md`.** That is the ONE handover file — a living document, updated in place. **Do not create dated handover siblings**; the 33 that used to exist were consolidated into it on 2026-08-12 and deleted (recoverable via `git log --diff-filter=D --name-only -- docs/handovers/`). It carries current state, the open queue, working discipline, traps, and restart recipes. **As of 2026-08-22 its top block is `START HERE — 2026-08-22 session close`, HEAD `992b96f`.** Read it before touching the NVDA glide, the plan pipeline, or anything tax-related. It records the Section-102 model as SETTLED (the grant benchmark is the 30-trading-day mean; the broker basis is the wrong basis; both slices fall due at sale; nothing is unreserved — three independent confirmations, including the 2025 Form 106) and carries the ONE item waiting on Ariel: whether he still wants the fast glide now that the tax is ILS 1,849,929 rather than the ILS 1,274,268 he was first told. It also flags that the working tree is dirty on purpose — three `domain_knowledge/` files hold Ariel's uncommitted 2026-08-21 moonshot-sleeve US-situs carve-out; read that diff before committing or discarding it.

**Two rules that cost seven plan drafts to learn (2026-08-16).** **(1) AMEND, never full-regenerate.** Use `plan_amendment.workers._medium_worker` (Phase 3 only, ~8 min) on named sections; a full regen re-writes all prose, so reviewers read a fresh document and raise a fresh set of objections — seven runs, no convergence. Amendments took Sol blockers 4 → 3 → 1.

  **CRITICAL CAVEAT (2026-08-23) — pick the TIER by whether you intend to PROMOTE.** `_medium_worker` is Phase 3 ONLY. It never reaches phases 4/4.5/5/5.5, so it writes **no `synthesis.phase_45` (codex) and no `synthesis.phase_55` (reader) rows** — and `promote_gate.evaluate_promotion` fails closed without them. **A medium amendment can NEVER be promoted, no matter how good its content.** Worse, `run_codex_second_opinion` needs `analyst_reports_text` / `debate_outcomes_text` / `risk_verdict_text` — Phase 1/2/4 artifacts a medium run never produces — so the verdicts cannot be generated for that draft even after the fact. Cost of learning this on 2026-08-23: three medium passes (plans 117→118→119) with good, Sol-reviewed content that no authority could ever clear, ending in a proposal to override the gate.
  * **Prose/cosmetic fix you will NOT promote** → `_medium_worker` with `freeze_except`.
  * **Anything you intend to promote** → `_large_worker`, which calls `run_synthesis(guidance=…, existing_decision_run_id=…)` — the full path, ETA **15 min nominal**, and every gate runs.
  * **The correct correction loop** is: full synth → fleet raises findings → amend with those findings as `guidance` via the LARGE tier → gates re-run fresh → repeat → promote. Use `run_synthesis(reuse_phases_from_run_id=<prior completed run>)` to reuse the expensive phase 1–2 analysts/debates; **phases 3–5 always run fresh** because risk/FM/reader are the blind gates and are never reused. That is cheap on analysis and honest on review.
  * **Enter through `_large_worker`, never call `run_synthesis` directly.** Both run the identical 15 phases, but the worker reuses the amendment's own `DecisionRun` (`existing_decision_run_id`) so chat-turn -> amendment row -> draft is ONE audit chain, emits the `plan.amendment.*` events the UI listens for, and re-checks for mid-run cancellation. Calling the underlying function opens an unlinked `plan_revision` row instead — the draft is fine, the record of why it exists is not (done wrongly on 2026-08-23, run 447).
  * **A run of medium amendments starves the corrective tier.** `run_synthesis` auto-reuses phases 1-2 from the most recent completed FULL run; medium runs have none, so eight days of amendments (runs 404-440) left run 447 reusing analysts from run 379 of 2026-08-15 — predating the entire §102 correction. Derived numbers still come from the resolver, but the analyst NARRATIVE is stale. One more reason the large tier is the default for anything real.
  * **The "~80 min / ~$20" figure that made the full run look unaffordable was a BUG, not the design.** `critique_reconcile` repriced all 51 positions once per *finding* instead of once per round — ~30 min a pass, three passes a run. Fixed 2026-08-23 (`2d760db`). Do not avoid the full path on cost grounds without re-measuring.

  **Correction (2026-08-17): amendments did NOT freeze untouched sections — that mechanism never existed.** The only preservation was `prior_items_index`, which preserves *item_ids*, not prose; `_medium_worker` re-authored all three horizon bodies every run. Measured 106 → 107: `medium`/`short` had **zero** sections identical, and `cover_assumptions` / `fi_bridge` / `monte_carlo` were dropped outright. The plan was **eroding monotonically** — plan 92: 56,123 chars / 18 sections → 106: 50,107 / 16 → 107: 44,629 / 15, ~20% lost across the chain. Amendments were *cheaper*, never *narrower*. Real freezing now exists: pass `freeze_except={slugs}` (plus `freeze_baseline_plan_id`) to `_medium_worker`; it merges by SLUG against the named baseline before the tokenizer runs. Default `None` = old full-re-roll behaviour, so you must opt in. **Freeze against the plan you are amending — `prior_current` is `role='current'`, which here is plan 92 from July, not the live draft.** **(2) A claim of success must cite a command that exercised the REAL path, and its output.** Mocked-seam tests are not evidence: the ETF verdict path shipped with 31 green tests and had never once run. Use `scripts/check_real_seam.py` and `scripts/smoke_real_paths.py`. Related: figures written as digits drift on every regeneration; bind them to `{{fact:<key>}}` tokens instead.

**Verify open items against `git log` and by EXECUTING the code before acting.** This repo's docs have twice asserted things the code contradicted — a "still unfixed" precondition that had been fixed for weeks, and an SDD paragraph describing a fail-closed gate that failed open. Read a comment, then run the function.
