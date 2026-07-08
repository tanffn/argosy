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

**Fresh session: start at `docs/handovers/2026-07-08-next-round-queue.md`** — Ariel's reviewed queue: (1) DESIGN critique-FED re-synthesis (corrections in, never from zero) BEFORE running the pending re-synthesis (which must clear 9 aggregated critique findings + apply glide-verdict proposal 49); (2) wealth-trajectory derived cache (slow); (3) plan-tab NVDA-share-trajectory vs Deconcentration consistency; (4) /retirement "Couldn't load your plan story" error; (5) present proposal-49-vs-current-glide comparison; (6) JobView DTO/JobsTable audit (home crash root). Also: state_snapshots UNIQUE collision, overdue action items missing from greeting needs_you, first full autonomous daily cycle → verify-run. Prior context:

Prior: `docs/handovers/2026-07-07-scheduler-audit-sdd-refresh.md` (read UPDATE + §3b–3d) — dead loop tier fixed (adoption), phases A/B/C1 shipped, FM greeting home LIVE (3 iteration rounds with Ariel), SDD user-AGNOSTIC rule + overhaul, Discord off. Root causes: "claude.exe not found" = **Windows 32,767-char argv cap** (fix: --system-prompt-file >8k, `3b9ae78`); plan_critiques EMPTY FOREVER (graph plans have raw_markdown='' → weekly_review no-op'd; fix `ea6d85b`) — **first critique landed: 2 RED cross-surface findings OPEN for Ariel** (IPS prose says NVDA 12% vs plan 8%; dashboard FX 3.00 vs plan 2.944); phantom $98k directive + buy-NVDA rebalance both stale-data bugs, fixed + superseded. **NVDA pace BEHIND (1,600 vs 4,775 sh YTD) while weight is on-waypoint — fleet adjudication needed (directive sell-half).** Full suite HUNG at `tests/test_api_phase4.py` first test (845 pass, 0 fail before it) — fix the hang, then overnight suite with pytest-timeout. Also open: catchup KeyError startup race; critique domain-source plumbing; bank DPYA; ~$9.6k EXUS tranche; next ingest auto-verifies armed expectations. Handover §3b + §4, then `git log --oneline -30`.

Prior: `docs/handovers/2026-07-06-resume-fixes-multi-turn-parse-and-detector.md` — funding breakdown renders in the deploy UI; debate-agent `recovered_from_scan` ~100% root-caused (`_select_response_text`); hallucinated-sources detector grounded. Before that: `docs/handovers/2026-07-06-fleet-live-e2e-and-plan-v67.md` — plan **v67 current** (x10 moonshot sleeve), **$161k deployed across 11 broker fills**, exit-1/inbox/cost/config root causes, analysts have WebSearch, TSV is OUTPUT-only.

Prior milestone (`4a7b782`): plan/execution separation (SDD §1.7) + incremental plan-refinement core + allocation-in-graph/mutation API — `docs/handovers/2026-07-03-plan-refinement-and-execution-separation.md`.
