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

**Fresh session: start at `docs/handovers/HANDOVER.md`.** That is the ONE handover file — a living document, updated in place. **Do not create dated handover siblings**; the 33 that used to exist were consolidated into it on 2026-08-12 and deleted (recoverable via `git log --diff-filter=D --name-only -- docs/handovers/`). It carries current state, the open queue, working discipline, traps, and restart recipes.

**Two rules that cost seven plan drafts to learn (2026-08-16).** **(1) AMEND, never full-regenerate.** Use `plan_amendment.workers._medium_worker` (Phase 3 only, ~8 min) on named sections; the full 15-phase run (~80 min, ~$20) is for strategy changes only. A full regen re-writes all prose, so reviewers read a fresh document and raise a fresh set of objections — seven runs, no convergence. Amendments freeze untouched sections and took Sol blockers 4 → 3 → 1. **(2) A claim of success must cite a command that exercised the REAL path, and its output.** Mocked-seam tests are not evidence: the ETF verdict path shipped with 31 green tests and had never once run. Use `scripts/check_real_seam.py` and `scripts/smoke_real_paths.py`. Related: figures written as digits drift on every regeneration; bind them to `{{fact:<key>}}` tokens instead.

**Verify open items against `git log` and by EXECUTING the code before acting.** This repo's docs have twice asserted things the code contradicted — a "still unfixed" precondition that had been fixed for weeks, and an SDD paragraph describing a fail-closed gate that failed open. Read a comment, then run the function.
