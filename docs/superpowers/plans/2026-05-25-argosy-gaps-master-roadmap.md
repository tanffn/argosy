# Argosy Gap Closure — Master Roadmap

> **For agentic workers:** This is a multi-wave roadmap, not a single-session plan. Each WAVE has its own detail spec; each DELIVERABLE within a wave is independently testable + reviewable + commitable. Use superpowers:subagent-driven-development per deliverable (code-writer + code-reviewer adversarial pair), then codex tandem as third-layer review before commit.

**Source:** `docs/design/argosy-gap-analysis-2026-05-25.md` (21 gaps across 5 layers, commit `32c9a67`).

**Goal:** Close the structural gap between the SDD's synthesis-flow vision and what runs today, so the `/plan` `[Run synthesis]` button produces a usable draft with per-agent visibility, per-position thesis cards, and retry-resume on failure.

**Total scope:** 30-50 hrs across 6 waves. Each wave ends in a verification checkpoint + user direction-check before the next wave starts.

## Tactical defaults applied (revisable later)

These open questions from §7 of the gap doc are getting *defaults* so we can move; user revises if wrong.

| Question | Default | Why |
|---|---|---|
| API key state | Assume keys may be missing; analysts must degrade to empty payloads gracefully. Wave 2 has a "what's actually configured" diagnostic deliverable. | Don't block on env state we can probe. |
| Cost cap | $10 soft cap per synthesis run; $30 daily soft cap for ariel. | SDD §3.10 budget is $5-8 per run; 2x headroom + abort if exceeded. |
| Decision tree UX | Reuse existing flat `AgentDetailDrawer` per-row drill-in for Wave 5; richer node-graph component is a Wave 6 sub-project. | The flat drawer is free after S2. Wait to design the richer tree until we see what we're trying to render. |
| Retry granularity | Wave 4 lands phase-level resume only (S3). Within-phase agent retry (S4) deferred to a follow-up. | Phase-level handles 80% of "lost work" cases; within-phase doubles implementation complexity. |

## Workflow per deliverable

Each deliverable in any wave follows this 4-step loop:

1. **Spec** — I write a `Goal / Owner / Proof` block (the code-writer's input contract): goal statement, files touched, acceptance test, test command + expected output.
2. **Implementation via code-writer** — dispatch the code-writer subagent with the spec. It internally pairs with code-reviewer (adversarial) up to MAX_ITERATIONS=3 until the diff passes review. Returns a unified diff or a `disagreement` (escalate to me).
3. **Codex tandem review** — I dispatch Codex (`role="reviewer"`) on the diff. Codex is third-layer independent review; returns LOOKS GOOD or BLOCKERS list with file:line cites.
4. **Apply + verify + commit** — apply the diff (already mostly applied by code-writer), run tests, address any Codex blockers, commit.

**Parallelism:** deliverables marked "P" can run in parallel (dispatched in a single message with multiple Agent calls). Sequential deliverables wait for predecessors.

---

## Wave 1 — Foundational (the "synthesis can produce a draft" wave)

**Goal:** A `[Run synthesis]` click produces a `role='draft'` PlanVersion with per-agent reasoning visible in the cascade drawer.

**Sequential within wave:** D1 → S1 → S2 (wait between each).

**Effort:** 6-10 hrs · **Cost:** ~$10-20 (1-2 verification synthesis runs)

| ID | Deliverable | Effort | Acceptance test |
|---|---|---|---|
| W1.A (D1) | Synthesis-specific input assembler. New module `argosy/orchestrator/flows/plan_synthesis/inputs.py` that produces a `Phase1Inputs` dataclass (or TypedDict) with all 9 analyst payloads. Reads portfolio via `argosy.ingest.tsv.parse_portfolio_tsv`; news/macro/social/fundamentals/indicators from existing adapters in `argosy/adapters/data/*` (Finnhub, FRED, TipRanks); fx_payload via `argosy/services/fx`; lots/dividends/RSU from empty defaults for now (D4/D5 fill them later). Each section degrades gracefully on adapter/network/missing-data error → empty payload. | 3-6 hrs | New test `tests/test_plan_synthesis_inputs.py` covering: each payload exists with correct keys; missing-adapter case yields empty payload; integration test shows phase 1 wired through it. |
| W1.B (S1) | Wire `_run_phase_1_analysts` (in `argosy/orchestrator/flows/plan_synthesis/orchestrator.py:365`) through W1.A. Replace the existing `common_kwargs` block (orchestrator.py:382) with construction from `Phase1Inputs`. Update `_safe_run_agent` (orchestrator.py:418) to forward only the relevant subset per agent class via `inspect.signature(agent.build_prompt).parameters` (existing narrow-kwargs pattern). | 1-2 hrs | Existing test `tests/test_plan_synthesis_flow.py::test_phase_1_runs_all_nine_analysts` still passes. New test confirms each of the 9 agents receives its required kwargs end-to-end. |
| W1.C (S2) | `agent_reports` persistence for synthesis-flow agents. In `argosy/agents/base.py::BaseAgent.run` (around line 662 where the `AgentReport` dataclass is constructed), persist the row to DB when `inputs.get("decision_id")` is set AND no other flow has already done so for this `run_correlation_id`. Pattern: take a callable `persist_fn` injected via subclass / module-level setter, OR write directly via `db_mod.get_session()` from inside the async path. Update WS finished payload (around line 696) to include the persisted `agent_report_id` (was None). Cross-check that advisor (`argosy/api/routes/advisor.py:_persist_turn:157`) and decisions (`argosy/decisions/flow.py:659`) flows don't double-write. | 1-2 hrs | New test: a synthesis run produces N `agent_reports` rows with `decision_id="plan-synth-N"` and matching `run_correlation_id`. Existing advisor/intake tests still pass (no double-write regression). |

**Wave 1 exit criteria:**
- `[Run synthesis]` click → cascade panel shows 13-24 agents streaming live
- Each agent's drawer shows its `response_text` (no longer empty)
- One verification synthesis run completes with `approved=true` OR fails with a documented, attributable reason (not a `TypeError`)
- Codex LOOKS GOOD on all 3 deliverables

**Pause point.** Look at the verification run's output. Did fund_manager approve? If not, why? Decide Wave 2 priorities based on what's still broken.

---

## Wave 2 — Cost safety + operational hygiene (parallel quick wins)

**Goal:** Make Wave 1 cost-safe to iterate and clean up operational paper cuts.

**Parallel:** all 4 deliverables can dispatch simultaneously (independent files).

**Effort:** 3-5 hrs · **Cost:** negligible

| ID | Deliverable | Effort | Parallelism |
|---|---|---|---|
| W2.A (S5) | Auto-retry-once for transient `claude.exe` exit-1 in `_call_via_claude_code_inner`. Detect signature: empty stderr + `ProcessError(exit_code=1)`. Gate retry on a per-turn flag so deterministic failure modes (e.g. encrypted PDF) don't double-cost. | 30 min | P |
| W2.B (S6) | Cost cap in `run_synthesis`. Track running cost across phases; abort with `RuntimeError("cost_cap_exceeded")` if > `$ARGOSY_SYNTHESIS_COST_CAP` (default $10). Surface `cost_so_far` in WS events for live UI display. | 1-2 hrs | P |
| W2.C (O2) | `_find_latest_tsv` restrict to canonical portfolio dir OR require "Bank account / funds allocation" header marker. Today's $0k bug regression test. | 15 min | P |
| W2.D (O3) | Startup-time orphan sweep. On uvicorn startup, mark any `decision_runs` with `status='running'` AND `started_at < now - 4h` as `status='failed', notes='orphaned by restart'`. | 30 min | P |

**Wave 2 exit criteria:** all 4 deliverables green; live verification confirms cost cap aborts a deliberate-runaway test.

---

## Wave 3a — Adapter probe + key wiring (small, gating)

**Goal:** Know what's actually configured before sinking time into D3/D4/D5.

**Effort:** 1-3 hrs · **Cost:** trivial

| ID | Deliverable | Effort | Parallelism |
|---|---|---|---|
| W3a.A | **Adapter probe diagnostic.** New CLI `argosy diagnose adapters` that pings each (Finnhub, FRED, TipRanks, CapitolTrades, SEC EDGAR) and reports OK / missing key / network fail. Tells us what's actually wired. | 1 hr | — (gating) |
| W3a.B (D2) | Wire missing API keys for adapters that probe found unconfigured. Document in `configs/` or env. May be a no-op if everything is already wired or genuinely unavailable. | 0-2 hrs (depends on probe) | — |

**Wave 3a exit criteria:** `argosy diagnose adapters` runs clean and prints a OK/missing/fail table. User decides which sources to wire before moving on.

---

## Wave 3b — Data backfills (per-source, mostly independent)

**Goal:** Fill empty operational tables so analysts have real material.

**Mostly parallel** (each source is independent).

**Effort:** 3 hrs - 2 days (depends on what 3a found unavailable) · **Cost:** API costs for first real runs

| ID | Deliverable | Effort | Parallelism |
|---|---|---|---|
| W3b.A (D3) | Persisted portfolio snapshots. New `portfolio_snapshots` table (migration 0030); writer fires on TSV ingest; `/api/portfolio/snapshot` reads from DB not filesystem. | 2-3 hrs | P |
| W3b.B (D4) | Tax lots + fills. Schwab CSV lot reader → `lots` table; reconcile sells/buys → `fills` table. | 4-8 hrs | P |
| W3b.C (D5) | RSU schedule. Either prompt-tweak intake_extractor to write `identity.rsu_grants:` OR a one-shot CLI to backfill from NVIDIA RSU portal screenshots. | 30 min – 2 hrs | P |

**Wave 3b exit criteria:** the empty tables identified in the gap doc (`news_cache`, `macro_cache`, `lots`, etc.) start filling on the next synthesis run; analysts produce non-trivial outputs.

---

## Wave 4 — Retry-resume + WS hardening

**Goal:** Failed synthesis runs don't lose work; can resume from last completed phase.

**Sequential:** W4.A → W4.B (UI depends on backend). W4.C parallel.

**Effort:** 8-14 hrs · **Cost:** ~$5-10 (resume verification)

| ID | Deliverable | Effort | Parallelism |
|---|---|---|---|
| W4.A (S3) | Per-phase output persistence. Extend `decision_phases` schema with `phase_output_json` blob column (migration 0031). Move `record_negotiation_phase` calls from end-of-flow to end-of-each-phase. New `POST /api/advisor/check-in/{decision_run_id}/resume` route that loads completed phases' outputs and resumes from first incomplete phase. | 6-10 hrs | — (foundational) |
| W4.B (P5) | UI retry button. On failed `synthesisError`, show `[Resume from phase N]` button. Wires to W4.A's resume route. | 1-2 hrs | — (after W4.A) |
| W4.C (O1) | WS `ws.send_failed` storm fix. Detect `state=disconnected` before send OR dedupe via send queue. Root cause investigation first. | 30 min - 2 hrs | P |

**Wave 4 exit criteria:** kill the backend mid-synthesis at phase 3, restart, click `[Resume]`, observe phases 1-2 are skipped and the flow resumes from phase 3. Cost of resume run = cost of remaining phases only.

---

## Wave 5 — Product surface (per-position UI)

**Goal:** Ariel's stated UX: "I have stock X → review → Hold/Sell/Buy + conviction + reasoning; I don't have Y → mention it WHY; overall portfolio health."

**Mostly parallel** since each card is independent.

**Effort:** 10-15 hrs · **Cost:** minimal

| ID | Deliverable | Effort | Parallelism |
|---|---|---|---|
| W5.A (P3) | Portfolio health card on `/plan`. Reads from `agent_reports` filtered by `agent_role='plan_critique' AND decision_id LIKE 'plan-synth-%'` (W1.C makes this persistence happen). Parses the `response_text` JSON for severity counts + top findings. **Explicit choice:** we read from `agent_reports` rather than re-persisting to the legacy `plan_critiques` table, because (a) W1.C makes agent_reports authoritative for synthesis output, and (b) `plan_critiques` is a side-table the synthesis flow has never used — adding a write to it would be a dead branch. | 1 hr | P |
| W5.A2 | **Brainstorming session** for per-position card shape. Before W5.B, run a focused brainstorm with mockups of: per-stock layout, what analyst evidence to surface, how to render Hold/Sell/Buy + conviction. | 1-2 hrs | — (gating for W5.B+) |
| W5.B (P1) | Per-position cards UI. New `/positions` route OR section on `/plan`. For each owned ticker, aggregate analyst verdicts from `agent_reports` filtered by `decision_id` + per-ticker fields. Render Hold/Sell/Buy badge + conviction + reasoning chain. | 3-5 hrs | — (after W5.A2) |
| W5.C (P2) | "Should add" candidates surface on `/proposals`. Renders `PlanSynthesisOutput.short.speculative_candidates` with WHY (synthesizer's rationale text). | 2-3 hrs | P (after W1) |
| W5.D (P4, P6) | Decision tree polish: ensure `<DecisionAccordion>` shows synthesis runs with correct grouping; verify cost values render correctly. | 1 hr | P |

**Wave 5 exit criteria:** the UX Ariel described is visible end-to-end. `/plan` and `/proposals` answer the questions a user would ask.

---

## Wave 6 — Stretch / nice-to-have

Deferrable; surface for future user direction.

| ID | Deliverable | Why deferred |
|---|---|---|
| W6.A | Richer decision-tree node-graph component (D3 / Mermaid / similar). | Existing flat drawer covers the core need. |
| W6.B (S4) | Within-phase retry (retry only failed agents inside a phase). | Phase-level resume (W4.A) handles 80%. |
| W6.C (L5) | Per-trade decision flow productionization. | Same data plumbing unlocks it; separate sub-project. |
| W6.D (O4-O6) | Handover gap fixes #1-3 (RSU shape, TLH constraint, DecisionAccordion empty). | Small individual fixes; bundle into a wave or land opportunistically. |
| W6.E | Argonaut snapshot / wave-X argument tracking. | Out of synthesis critical path. |
| W6.F | Insurance & pension coverage analysis wave. | Proposed wave already in handover; separate sub-project. |

---

## Cumulative effort + cost estimate

| Wave | Effort | Cost (API) | Cumulative effort | Calendar |
|---|---|---|---|---|
| 1 | 6-10 hrs | $10-20 | 6-10 hrs | 1 day |
| 2 | 3-5 hrs | $1-5 | 9-15 hrs | 1.5 days |
| 3a | 1-3 hrs | trivial | 10-18 hrs | 2 days |
| 3b | 3 hrs – 2 days | $5-20 | 13-34 hrs | 2-4 days |
| 4 | 8-14 hrs | $5-15 | 21-48 hrs | 3-6 days |
| 5 | 10-15 hrs | $1-5 | 31-63 hrs | 4-8 days |
| 6 | 8-20 hrs | $0-10 | 39-83 hrs | optional |

Realistic: **30-50 hrs of focused work, $25-65 in API costs, spread across 1-2 weeks of calendar time if working full-time on Argosy.**

---

## How we move

1. **Now:** I write Wave 1's detail spec (`docs/superpowers/plans/2026-05-25-wave-1-foundational.md`) and dispatch code-writer for W1.A.
2. After W1.A merges: dispatch code-writer for W1.B.
3. After W1.B merges: dispatch code-writer for W1.C.
4. Wave 1 verification run → look at output together → decide Wave 2 priorities.
5. Repeat for each wave.

**You can interrupt at any point.** Each wave is its own commit boundary; rolling back a wave is `git revert <range>`.

---

**Last edit:** 2026-05-25 by Claude. Trigger: Ariel's "do all" + "break into testable deliverables" + "subagents + codex" directive. Reviewed: codex tandem before commit.
