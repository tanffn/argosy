# Argosy — fresh-agent router

Multi-agent financial-advisor system. Python + FastAPI + Next.js. Single user today (Ariel + Noga); multi-tenant ready by design. This file exists to keep a fresh LLM from burning tokens skimming a ~70k-token SDD before doing useful work.

## Read these first (in order)

1. **`docs/design/SDD.md`** — the canonical design doc. Two sections matter upfront:
   - `## Handover note` (top of file, around line 16) — current wave state, open items, where things are. Self-stamped point-in-time; cross-check against `git log -20` if it feels stale.
   - `## Quickstart for new agents — where to find things` (around line 305) — task → file router (20 rows), 14-item conventions/gotchas list, filesystem layout, binding preferences. **This is the section to read for code investigation.**
2. **`graphify-out/GRAPH_REPORT.md`** — 30+ named architectural areas with their member files. Complements the SDD task→file table from the file→area direction. Run `graphify query "<question>"` for cross-file traces (~26× token reduction vs reading raw files).
3. **`~/.claude/projects/D--Projects-financial-advisor/memory/MEMORY.md`** (Claude Code only) — binding user preferences. The ones below are duplicated here so non-Claude agents see them too.

## Binding preferences (verbatim)

- **Accuracy over LLM cost.** Prefer thorough/accurate options over cheaper ones. Synthesizer + bull/bear/trader/fund_manager/audit/plan_synthesizer default to Opus; no Haiku defaults remain (see SDD §3.8).
- **Ask, don't assume.** Surface judgment calls to Ariel; don't auto-decide like prior LLM waves did.
- **`docs/design/SDD.md` is the only canonical SDD.** Do NOT edit `.docx` siblings or any other "SDD" file. If a tool offers to update both, decline.
- **Manual UI smokes are skipped.** Backend tests + live-LLM e2e are the verification surface; don't propose manual click-through plans.
- **Use the codex-tandem kit for risky work** (money math, parsers, migrations, decision flows). Skip for UI / lint / docs. Kit at `tools/codex-tandem/` (gitignored, NVIDIA-internal). See `reference_codex_tandem.md` in auto-memory for invocation patterns.

## Environment

- **Working dir:** `D:\Projects\financial-advisor\` (= `ARGOSY_HOME`).
- **Python venv:** `.venv/`. Interpreter: `D:/Projects/financial-advisor/.venv/Scripts/python.exe`. Use `uv sync` / `uv run`.
- **Shell:** PowerShell on Windows. `&&` doesn't chain — use `;`, or the Bash tool for POSIX scripts.
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

**Fresh session: start at `docs/handovers/2026-07-03-plan-refinement-and-execution-separation.md`** — this session shipped (to `master`, HEAD `4a7b782`) three arcs: (A) the **plan/execution separation** (SDD §1.7) + regime-data enrichment + a **blind anti-correlation gate** now enforced at `/accept` (a plan that breaches its own single-name look-through cap can't promote); (B) the **incremental plan-refinement decision core** (kernel invariants + node metadata + blast-radius T0/T1/T2 classifier + `run_refinement`); (C) **allocation-in-graph + the mutation API** — durable authored overrides ("option B", migration 0076, survive re-synthesis), `POST /api/plan/refine` (preview + apply→staged-draft, no auto-promote) + `POST /api/plan/rollback`. KEY functional finding: the live plan breaches its 13% cap at 16.8%, the sleeve-only fix makes it WORSE (18%), and it only clears by lowering the NVDA target (→8% ⇒ 11.3%). Every piece blind-reviewed + firsthand-verified (zigzag, don't rubber-stamp). NEXT = stage a cap-clearing refinement for Ariel; sigma-anchor invariant; scalar-node mutation; `/refine` UI. Read memory `feedback_plan_execution_separation` + `feedback_adversarial_review_must_re_derive_blind`, then `git log --oneline -30`. (Prior arc — the fleet-authors/determinism-verifies deployment author — is in `docs/handovers/2026-07-03-deployment-author-last-mile.md`.)

Prior milestone: proactive period directive + exposure-aware deployment + the 4-category NVDA sell protocol + the decision funnel exposed as beta (2026-07-02/03).
