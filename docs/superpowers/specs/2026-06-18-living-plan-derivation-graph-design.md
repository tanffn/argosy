# Living-plan derivation graph + change/adjudication substrate

## Problem

The synthesis flow is a **generator, not an editor**. Every run, phase 3 re-writes the
entire plan from scratch from analyst evidence + locked derived numbers; the current plan
is fed only as "soft reference." Consequences:

- **Non-determinism → whack-a-mole.** Because every run re-decides everything, settled
  content drifts and resolved contradictions reappear. We then spend a ~25-min reader pass
  catching contradictions our own regeneration introduced. (Proven: runs 106/107 could not
  converge; the surgical-reconcile sub-step was added precisely because re-running the full
  synthesizer "reshuffles & re-introduces defects → does not converge.")
- **Cost.** A full run is ~95 min even when one input changed.
- **Wrong mental model.** A client asking to fix one thing should not trigger a blank-page
  rewrite. The plan should be a **living artifact**; a change should update what it touches
  and nothing else.

## Goal

The plan is a **derivation graph**. A change — from the **user OR an agent** — lands on a
node; **only the transitive dependents recompute and re-render; everything outside the
blast radius stays byte-identical.** "Settled" is not a lifecycle lock anyone sets; it is the
emergent property *"not downstream of this change."* You can always relaunch with new
information; the system computes the blast radius instead of regenerating the world.

This is the natural endpoint of derivation-first (we already have input-vs-derived typing in
`plan_model`, derivation recipes in `rederivation_reviewer`, source-locators in the resolver)
and it subsumes the negotiation-substrate work (an FM objection / reader finding / codex
block is just a change-request on a node).

## Architecture — two layers over one graph

### Layer 1 — the derivation graph (the plan AS a DAG)

**Node kinds:**
- **InputFact** (X): holdings snapshot, a vest, a tax-sim lot set, a goal, a rate, a user
  constraint, an analyst observation. The graph's *sources*. Authored (user or ingest or an
  analyst agent), never derived.
- **DerivedValue** (Y, Z): FI margin, NVDA target/sell, retirement age, US-situs estate, …
  Computed *from* specific inputs by a **recipe**. Never hand-edited.
- **Surface**: a rendered consumer — a prose section, a dashboard tile, an appendix table,
  the FI verdict. Consumes derived values (and sometimes inputs) and renders text/markup.

**Edges = "derived_from", built HYBRID (per user decision):**
- **DerivedValue → declared recipe.** A recipe declares its input keys; those keys ARE the
  inbound edges. We already have these in `rederivation_reviewer.standard_recipes()` and the
  resolver's per-key derivation; this design promotes that recipe registry to the canonical
  edge source and extends it to every derived headline value.
- **Surface → inferred from stored citations.** Each surface already records the facts it
  cites (`sections_json` evidence, the resolver `source_locator`s). Each citation is an
  inbound edge. A surface fact with NO citation = a missing edge = fail-closed (see Risks).

**Each node carries:** `id`, `kind`, `value`/`content`, `provenance` (author + recipe or
source), `input_hash` (hash of its inbound nodes' current values), `status`. `status` is
emergent: a node is **valid** iff `input_hash` == the hash at last compute; else **stale**.
A node flagged by an open change-request is **flagged**. No manual freeze.

### Layer 2 — the change / adjudication substrate (author-agnostic)

A **ChangeRequest** is the single primitive any author writes:
`{ target_node, author (user | agent_role), kind, payload (new input value | proposed
recipe/policy | objection), rationale }`.

**Adjudication is ownership- and authority-specific, fail-closed:**
- **InputFact target** — owned by the user / ingest pipeline / the analyst that produces it.
  A new value is accepted as a fact (it IS ground truth) and marks the node dirty.
- **DerivedValue target** — **not directly editable by anyone.** A change-request that tries
  to set a derived value is rejected with "change the inputs or the recipe." This preserves
  derive-don't-inherit and means no agent (or user) can hand-write a derived number.
- **Recipe / policy node** (e.g. the NVDA cap, the SWR, a phase assumption) — owned by ONE
  authority (e.g. concentration analyst owns the cap). Another author (FM, reader) may
  **object** → the request routes to the owner, who re-derives or escalates to the user.
  Typed terminal states: `accepted` / `rejected` / `superseded`. **Hard nodes
  (math/derivation/coherence) can never be "agreed away"** — they recompute from inputs;
  an objection to a hard node is only resolvable by changing an input or fixing the recipe.

The FM, reader, and codex stop being "approve/reject the whole plan." They emit
change-requests on specific nodes. The whole-artifact reader's contradiction findings become
change-requests targeting the offending **Surface** nodes.

### Layer 3 — incremental recompute + propagation

On accepting a change to node N:
1. Mark N dirty.
2. Topologically walk N's **transitive dependents**; mark each stale.
3. Recompute each stale **DerivedValue** from its recipe (deterministic).
4. Re-render each stale **Surface** from its now-updated cited values. Pure-render surfaces
   (dashboard tiles, appendix tables, the FI verdict) are re-rendered deterministically.
   Free-text prose surfaces use the existing converge-safe **surgical editor**
   (`surgical_reconcile`: edits the cited span, forbidden from introducing new numbers, so
   it can't reshuffle the rest).
5. **Everything outside the closure is reused byte-identical.**
6. **Re-verify only the blast radius:** the deterministic gate + a whole-artifact read run
   over the changed surfaces, not the full 95-min fleet.

Analysts re-run **only** when a change targets an input they own and that input is stale —
not all 11 from zero (Layer-3 follow-on; see Phasing).

### Layer 4 — migration / coexistence

- **Hydrate the graph from the CURRENT plan** (the accepted plan / latest draft) as the base:
  derived-value nodes from the resolver manifest, surface nodes from `sections_json`, edges
  from recipes + citations. Hydration must reproduce the existing surfaces (round-trip test).
- **Keep the from-scratch synthesizer** as (a) the cold-start path (new user, no plan) and
  (b) an explicit "full rebuild" escape hatch. **Steady state = incremental.**

## Data model

- `plan_nodes`: `id`, `plan_id`, `node_key`, `kind` (input|derived|surface), `value_json` /
  `content`, `input_hash`, `status` (valid|stale|flagged), `provenance_json`, `owner`.
- Edges: derived at load from the recipe registry (derived) + stored citations (surfaces);
  optionally materialized in `plan_edges` for query/audit.
- `change_requests`: `id`, `plan_id`, `target_node_key`, `author`, `kind`, `payload_json`,
  `rationale`, `status` (proposed|accepted|rejected|superseded), `adjudicated_by`,
  `terminal_reason`, timestamps.

## How a steady-state run works

1. Collect pending ChangeRequests (user inbox + agents that ran this cycle).
2. Adjudicate each (ownership + fail-closed hard nodes) → accepted set.
3. Apply accepted input/recipe changes → mark dirty.
4. Propagate (Layer 3): recompute dirty closure; re-render dependent surfaces; reuse the rest.
5. Re-verify the blast radius (gate + reader on changed surfaces only).
6. Converges by construction — unaffected content cannot reshuffle.

## Testing

- **Graph correctness:** recompute is deterministic given inputs; invalidation closure is
  exact (change X ⇒ exactly {X + transitive dependents} stale; everything else valid).
- **Adjudication:** a change-request that sets a DerivedValue is rejected; an FM objection on
  the cap routes to the owner; a hard node cannot be agreed away.
- **Propagation:** changing one input re-renders only dependent surfaces; all other surfaces
  byte-identical (diff == ∅ outside the closure).
- **Migration round-trip:** hydrating the graph from an existing plan reproduces its surfaces.

## Risks / edge cases

- **Citation completeness.** If a surface's citations are incomplete, an edge is missed and a
  dependent surface won't recompute (silent staleness). Mitigation: the gate already enforces
  every headline number traces to a resolved value (`HEADLINE_NUMERIC_SOURCE`); extend it so
  every surface fact must carry a citation → an uncited fact is a missing edge and a
  **fail-closed block**, not a silent miss.
- **Prose surfaces are not pure functions** (LLM edits are stochastic). Mitigation: the
  surgical editor already converges (span-local, no new numbers); pure-render surfaces avoid
  the LLM entirely.
- **Cycles.** The graph must be a DAG; detect cycles at hydration and fail loud.
- **Cross-cutting conclusions** (the FI verdict depends on many inputs) are modeled as a
  DerivedValue/Surface with many inbound edges — correctly in the blast radius of any of them.
- **Concurrent change-requests** to overlapping closures: adjudicate in one cycle, apply as a
  batch, recompute the union closure once (avoid thrash).

## Phasing (each its own plan)

1. **Graph + propagation core** (this spec's heart): node model, hybrid edges, deterministic
   recompute, exact invalidation, migration/hydration, blast-radius re-verify. Surfaces:
   start with deterministic-render surfaces + the surgical editor for prose.
2. **Change/adjudication substrate**: ChangeRequest table, ownership map, fail-closed
   authority clearance, FM/reader/codex emit change-requests instead of whole-plan verdicts.
3. **Scoped agent re-runs**: an analyst re-runs only when its owned input is stale.

Non-goals for sub-project 1: re-running analysts incrementally (phase 3); a UI for authoring
change-requests (the user's existing intake + the agents are the authors initially).
