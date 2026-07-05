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
- **The LLM TEAM is the architecture — not deterministic gates.** A *judgment* failure (bad rationale, wrong instrument, imprudent call) is caught by ANOTHER agent that re-derives blind — never by adding a per-symptom deterministic gate (that's the whack-a-mole antipattern). Determinism is the **inviolable-arithmetic floor only** (conservation, estate/us-situs); it never judges "is this a good decision." Default response to a bad decision = *"which agent should have caught this, and why didn't it — fix the team (inputs / blind-review / reliability)."* Adding a gate for a judgment failure is the exception and needs explicit justification to Ariel. The team was once cut for timing out — the fix is the P0 reliability wrapper, NOT retreating to determinism. See memory `feedback_fleet_authors_determinism_verifies` (2026-07-04 correction).

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

**Fresh session: start at `docs/handovers/2026-07-05-llm-team-architecture-and-fleet-reliability.md`** — shipped to `master` (HEAD `2cba7cc`): (A) a binding **direction correction** — *the LLM team IS the architecture, not deterministic gates*; a JUDGMENT failure is caught by another agent re-deriving blind, determinism is the inviolable-arithmetic floor only (encoded in `CLAUDE.md` + memory `feedback_fleet_authors_determinism_verifies`); (B) the **deploy decision team** (author → blind concentration/diversification/prudence reviewers → flagged-not-shipped), wired into `get_deploy_cash`, **proven live catching R1GR (BLOCK) + CSPX (WARN)** by judgment; (C) the **per-stock research→verdict** capability (HOLD first-class & silent) — live fetchers + daily job + blind-verify gate; (D) a **permanent high-growth plan sleeve** (codex-verified σ) with **draft v64 staged** for review; (E) deploy FX/UCITS-quote fixes. **#1 NEXT (blocking):** the fleet dies on transient `claude.exe exit-1` with NO retry wrapper (the ACN `/consult` blocked twice, runs 122/123, on quorum) → generalize the P0 reliability wrapper (`allocation_author/reliable.py`, currently author-only) to the consult analysts + team reviewers. Read memory `feedback_fleet_authors_determinism_verifies` (2026-07-04/05 corrections) + `feedback_adversarial_review_must_re_derive_blind`, then `git log --oneline -20`.

Prior milestone (`4a7b782`): plan/execution separation (SDD §1.7) + incremental plan-refinement core + allocation-in-graph/mutation API — `docs/handovers/2026-07-03-plan-refinement-and-execution-separation.md`.
