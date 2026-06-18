# Incremental Plan Pipeline (Capstone / Cutover) Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Wire the built components (engine + collections + hydration + persistence + surfaces + adjudication/ladder + publish_gate) into ONE runnable incremental cycle that takes the current plan + a set of change-requests (from the user OR agents), updates only the blast radius, re-renders canonical surfaces, re-verifies coherence, and ends CLOSED (promotable) or with a FINITE set of real client questions — proven on Ariel's real data. Gated behind `ARGOSY_INCREMENTAL_PLAN` (does NOT rip out from-scratch synthesis; flipping the default is the final, separately-approved step).

**Architecture:** A new flow `argosy/orchestrator/flows/incremental_plan.py` exposing `run_incremental_cycle(session, *, user_id, change_requests=None, participants=None, persist=True) -> CycleResult`. It composes existing modules; it adds NO new derivation math (reuses the resolver-backed recipes from `graph_collections` + `graph_hydration`).

**Tech Stack:** Python; the committed graph stack; pytest (fakes for the ladder LLM seam).

---

### Task 1: `CycleResult` + `build_base_graph` (hydrate current plan into one graph)

**Files:** Create `argosy/orchestrator/flows/incremental_plan.py`; Test `tests/test_incremental_plan.py`.

`build_base_graph(session, user_id) -> DerivationGraph`:
- collections via `graph_collections.build_holdings_graph(positions, fx)` from the latest snapshot (read-only).
- resolver-manifest derived nodes via `graph_hydration` (FI margin liquid, earliest_safe_age, etc.).
- canonical surfaces via `live_surfaces.register_canonical_surfaces(graph, CANONICAL_SUBJECT_NODE)`.
- `recompute()`; assert `is_closed()`.

`CycleResult` dataclass: `closed: bool`, `real_questions: list[dict]`, `open_flags: list[str]`, `recomputed: list[str]`, `replay_ref: str | None`.

- [ ] Step 1: failing test — `build_base_graph` on a hermetic fixture (seed a minimal snapshot) returns a graph where `is_closed()` and the FI/age/us_situs canonical surface nodes exist + are valid.
- [ ] Step 2: run → fails (module missing).
- [ ] Step 3: implement `build_base_graph` + `CycleResult` composing the existing builders.
- [ ] Step 4: run → pass.
- [ ] Step 5: commit `feat(incremental): build_base_graph hydrates current plan into one closed graph`.

### Task 2: change-requests → adjudication → ladder → apply

`_apply_change(graph, cr, participants) -> AppliedChange`:
- `change_adjudication.adjudicate(cr, ownership)` → REJECTED (derived target) / NEEDS_AUDIT (input) / NEEDS_LADDER (recipe/policy).
- NEEDS_LADDER → `negotiation_ladder.run_ladder(cr, participants)`; terminal `arbiter_ruled`/`*_conceded` → apply if it resolves to an input/recipe change; `escalated_to_user` → collect into `real_questions` (do NOT apply).
- accepted input change → `graph.set_input(node_key, value)`.

- [ ] Step 1: failing tests (fakes for participants): a derived-target CR is rejected; an evidence-resolvable CR applies + changes the node; a genuine-decision CR yields a `real_question` and leaves the graph unchanged.
- [ ] Step 2–4: implement + green.
- [ ] Step 5: commit `feat(incremental): change-request -> adjudicate -> ladder -> apply`.

### Task 3: propagate + re-render + global coherence recheck

`run_incremental_cycle`:
1. `graph = build_base_graph(...)`.
2. for each CR: `_apply_change(...)` (collect real_questions, accumulate dirtied inputs).
3. `recomputed = graph.recompute()` (blast radius only).
4. re-render affected surfaces (already recomputed as SURFACE nodes).
5. `surface_rendering.recheck_coherence(graph)` over the whole artifact → `open_flags`.
6. `closed = is_closed() and not open_flags and not real_questions`.
7. if `persist`: save via `graph_store` + emit a `propagation_events` row (replay_ref).

- [ ] Step 1: failing test — a cross-surface scenario: change the fi_margin input; assert BOTH the FI verdict surface and the dashboard FI tile updated identically (no basis-flip) and `recheck_coherence` returns no flags; `closed is True`.
- [ ] Step 2–4: implement + green.
- [ ] Step 5: commit `feat(incremental): propagate + re-render + global coherence recheck -> CycleResult`.

### Task 4: publish gate + real-data demonstration script

- Wire `publish_gate.can_publish_plan` into `run_incremental_cycle` to compute promotability from the open_flags + the promote_gate authorities (codex/gate/FM/reader/rederivation), fail-closed.
- Create `tmp_review/demo_incremental.py`: build the base graph for `ariel` from the LIVE DB (read-only), feed the outstanding reader/codex findings from the latest blocked draft as change-requests, run the cycle, and write a UTF-8 report: closed? which surfaces are now consistent (FI basis, age), the per-symbol US-situs list, and any real client questions. NEVER print the shekel sign to the console.

- [ ] Step 1: failing test for the publish-gate wiring (a graph with an open hard flag is not promotable; a clean one is).
- [ ] Step 2–4: implement + green.
- [ ] Step 5: run `demo_incremental.py` on ariel's real data; capture the report. Commit `feat(incremental): publish-gate wiring + real-data demonstration`.

---

## Acceptance (the END for this push)
- `run_incremental_cycle` on Ariel's real data ends **CLOSED** OR with a **finite list of arbiter-certified real client questions** — and the FI-basis / retirement-age cross-surface contradictions that blocked draft 50 are **gone by construction** (one node → many surfaces). Demonstrated in `tmp_review/demo_incremental_report.txt`.
- Behind `ARGOSY_INCREMENTAL_PLAN`; from-scratch synthesis untouched. Flipping the default = the final step, surfaced for explicit approval.

## Self-review hooks
- Money-math: `graph_collections` recipes must equal the resolver's values for the current snapshot (codex-reviewed in wave 4).
- No real claude.exe in tests (ladder participants are fakes); the real demo run may use real participants but must be bounded.
