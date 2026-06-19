# Generator-swap spike — findings (2026-06-19)

**Question:** how does an edit-in-place (incremental) plan become a CLEAN PROMOTED plan the
user sees in the app — given the current `can_publish_plan` authority model expects synthesis-run
verdicts (codex p45 / reader p55 / FM) that an incremental cycle does not produce?

## Answer (from the design spec + code audit)
The spec (`2026-06-18-living-plan-derivation-graph-design.md`, Layers 3–4) is explicit:
- **Promotion runs the FULL authority set, SCOPED — never inherited.** codex re-derivation on the
  *changed* derived-value nodes + a whole-artifact reader pass + FM + deterministic gate +
  rederivation. Targeted, not a full 95-min fleet, but never dropped. So an incremental plan
  **earns** clearance by RUNNING the (scoped) authorities at promotion — it does not magically clear.
- **Hydrate the current plan → graph as the base** (graph_hydration), surgically edit, re-render.
- **Per-change coherence recheck (cheap: deterministic gate + reader pass) is NOT the promotion
  authority** — it catches contradictions continuously; promotion still runs the full gate.
- **Keep from-scratch synthesis as cold-start + full-rebuild escape. Steady state = incremental.**

## Built vs missing (code audit)
BUILT: graph engine (`derivation_graph`), hydration (`graph_hydration` — manifest scalars +
sections_json → DERIVED/SURFACE nodes), canonical surfaces (`live_surfaces`), collections
(`graph_collections`), change-adjudication (`change_adjudication`), negotiation ladder
(`negotiation_ladder` + experimental `RealLadderParticipants`), publish gate (`publish_gate`
wraps `promote_gate`), `run_incremental_cycle` (composes the above; authorities are an INJECTED
param — at `/accept` it reads them from the synthesis run's phases).

MISSING (this is the generator swap):
1. **graph → plan_version render bridge.** No function renders a publishable plan artifact (full
   prose/sections/allocation/theses) FROM the edited graph. Hydration is plan→graph only; surfaces
   render individual node text, not a full artifact. Without this there is nothing to promote.
2. **Scoped authority RUN.** No code runs codex re-derivation / reader / FM *scoped to the changed
   nodes* and emits their verdicts at promotion. `run_incremental_cycle` consumes authorities; it
   does not produce them. This is the expensive, load-bearing piece (real agent calls, targeted).
   The gap-1 ladder OWNER agent is part of this (negotiating recipe/policy changes).
3. **Trigger routing.** The `plan_synthesis` job (fired by `replan_dispatcher` /
   `monthly_cycle` / `state_observer` / `advisor` / amendments) still calls `run_synthesis`. The
   swap routes eligible (node-localized) triggers to the incremental path; structural/cold-start
   stays on synthesis.
4. **Flag-gated reversible rollout.** Today `ARGOSY_INCREMENTAL_PLAN` gates only `/accept`. The
   generation-side swap needs its own flag + synthesis-fallback, like the promotion cutover.

## Honest scope
This is NOT a quick wire-up. It is essentially building the steady-state Layer-3/4 pipeline, and
the scoped-authority-run (#2) reintroduces real (targeted) agent cost. It is a multi-subsystem,
multi-session effort. Each of #1–#4 is its own plan that produces working, testable software.

## Recommended milestone sequence (each = its own plan)
- **M1 — graph → plan_version render bridge** (no live wiring; produces a coherent full artifact
  from the hydrated+edited graph; testable in isolation). Highest value first: it's the thing that
  turns "the engine is coherent" into "a full plan artifact exists," and it's pure/deterministic.
- **M2 — scoped authority run** (codex re-derivation on changed nodes + reader pass + FM via the
  ladder owner agent / gap-1). The expensive core; needs a live verification run.
- **M3 — trigger routing + flag-gated rollout** (route node-localized triggers to the incremental
  path; synthesis stays fallback; reversible).
- **M4 — observability** (threaded change-request view + blast-radius diff over Replay).

A clean PROMOTED plan in the app first becomes possible at the end of M2 (render + authorities
clear), wired live in M3.
