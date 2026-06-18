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
  constraint, a tax-year, an FX rate, a legal/policy assumption. The graph's *sources* —
  authored by the user or ingest, never derived. (An **analyst *observation* is NOT an
  InputFact** — it depends on holdings/taxes/goals, so it is a DerivedValue/observation node
  with its own inbound edges; only its irreducible judgment component is an input. This was
  codex-flagged: treating observations as sources breaks invalidation.)
- **DerivedValue** (Y, Z): FI margin, NVDA target/sell, retirement age, US-situs estate, …
  Computed *from* specific inputs by a **recipe**. Never hand-edited.
- **Surface**: a rendered consumer — a prose section, a dashboard tile, an appendix table,
  the FI verdict. Consumes derived values (and sometimes inputs) and renders text/markup.

**Edges = "derived_from", built HYBRID (per user decision) — but edges are not only named
scalar keys (codex Theme A: declared keys + citations miss real dependencies):**
- **DerivedValue → declared recipe, including SET/membership edges.** A recipe declares its
  inputs as (a) named keys AND (b) **set queries** — "all taxable lots", "all goals", "all
  future vests", "all USD cashflows". A set-query edge invalidates the node when ANY member
  is added/removed/changed (membership-hash, not just the listed scalars), closing the
  "new lot added but not in the static key list → stale-but-valid" hole. We promote
  `rederivation_reviewer.standard_recipes()` to the canonical registry and extend every
  headline derivation with explicit set edges.
- **Surface → inferred from citations PLUS declared structural dependencies.** Citations
  prove provenance but not everything that made a *qualitative* claim appropriate
  ("largest risk", "no estate action needed", sorted tables, *omitted* holdings, thresholds,
  comparisons, absences). So a Surface declares, in addition to its citations: the
  **set/threshold/absence predicates** it asserts (e.g. "max over {risks}", "count of
  US-situs holdings == 0", "ranked by X"). Those predicates are edges to the underlying sets.
  A surface fact with neither a citation nor a declared predicate = a missing edge =
  fail-closed (see Risks).

**Each node carries:** `id`, `kind`, `value`/`content`, `provenance` (author + recipe or
source), `input_hash`, `status_validity`, `status_flag`. The `input_hash` covers **both the
inbound node values/membership AND the compute provenance** — recipe code version, render
template version, model/prompt version, schema version, and any tax-law/policy version the
recipe reads (codex Theme A: a value can go stale because the *computation* changed even when
inputs did not). `status_validity` is emergent (`valid` iff `input_hash` == hash at last
compute, else `stale`); `status_flag` is **orthogonal** (`none` | `flagged-by-open-change-
request`) — a node can be both stale AND flagged. No manual freeze.

### Layer 2 — the change / adjudication substrate (author-agnostic)

A **ChangeRequest** is the single primitive any author writes:
`{ target_node, author (user | agent_role), kind, payload (new input value | proposed
recipe/policy | objection), rationale }`.

**Adjudication is ownership- and authority-specific, fail-closed:**
- **InputFact target** — owned by the user / ingest / the producing analyst. A new value is
  accepted as a fact and marks the node dirty. **Anti-laundering (codex Theme B):** an input
  change that would flip a hard verdict is itself an adjudicated, audited change-request — it
  must carry evidence, and the node's owner (or the arbiter) can reject an unjustified premise
  change. You cannot get a desired conclusion by quietly editing a premise; the premise edit
  is on the record and contestable.
- **DerivedValue target** — **not directly editable by anyone.** A request to set a derived
  value is rejected with "change the inputs or the recipe." Preserves derive-don't-inherit.
- **Recipe / policy node** (NVDA cap, SWR, a phase assumption) — owned by ONE authority. A
  change to it routes through the **negotiation ladder** below.

**The negotiation ladder (per user decision) — a change-request is a negotiation, not a
command:**
1. **A files** "change X because Y" against a node **B owns**.
2. **B may push back on the rationale Y itself**, not just the value. The burden is on A to
   defend Y; "A said so" never wins. (B's pushback is the live defense against laundering — a
   bogus Y is rejected here.)
3. **Peer round A ↔ B, bounded to n = 3** (generalizes the existing
   `converge_fm_objections` dialogue to any author pair).
4. **Unresolved after n = 3 → escalate to the arbitration agent (FM).** The **arbiter
   classifies the conflict**: *resolvable by evidence/derivation* → it rules and applies (re-
   derive / fix input / pick the better-supported Y), staying inside the fleet; *a genuine
   judgment call* (goal / risk-tolerance / tradeoff, not settleable by data) → escalate up.
5. **Escalate to the user — last rung, only for a certified real decision** — presented as a
   single clear boxed choice, never a vague "you decide." (Honors `agents_talk_to_each_other`,
   `argosy_picks_the_solution`, `ask_dont_assume`.)

Typed terminal states on the change-request: `A_conceded` / `B_conceded` / `arbiter_ruled` /
`escalated_to_user` / `superseded` — recorded so a settled dispute cannot silently reopen.

**Hard nodes (math/derivation/coherence) are never "agreed away"** — they recompute from
inputs; the only resolutions are changing an input (adjudicated, see anti-laundering) or
fixing the recipe. **A Surface contradiction is treated as a SYMPTOM first (codex Theme B):**
the reader's finding routes to the *root* — if the contradiction traces to a wrong derivation
or inconsistent input, that is fixed and the surface re-renders; a surgical prose patch is
permitted ONLY once the underlying derivation is verified consistent, so a local edit can
never make the artifact *look* coherent while the graph stays wrong.

The FM, reader, and codex stop being "approve/reject the whole plan." They emit
change-requests on specific nodes. The reader's contradiction findings become change-requests
that first interrogate the derivation, then (if clean) the offending **Surface** node.

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
5. **Everything outside the closure is reused byte-identical** — but only for surfaces that
   are *truly independent*. Shared narrative state (terminology, recommendation priority, a
   global caveat) is modeled as its own node so a change to it correctly invalidates every
   surface that consumes it; byte-identity is never assumed for surfaces sharing such a node
   (codex Theme C).
6. **Re-verify: blast radius for the EXPENSIVE checks, GLOBAL for coherence (codex Theme C).**
   A changed surface can contradict an *unchanged* one outside the closure, and that
   contradiction edge is not in any citation. So cross-surface coherence is rechecked
   **globally** every change — but cheaply: the deterministic coherence gate over the whole
   artifact (sub-second) + one whole-artifact reader pass (minutes), NOT the 95-min fleet. The
   recompute/re-render is scoped; the *coherence verdict* is whole-artifact.

**Graph expansion (codex Theme A):** a structural change — a new holding, goal, account, or
tax lot — does not only mark existing dependents stale; it **creates new nodes and edges**
(the new holding's value node, its surface rows, its set-membership in every set-query edge).
Propagation runs over the *expanded* graph, not a static closure.

**Publish blocker — ONE verification surface for steady-state AND migration (closes the
codex re-review's asymmetry finding):** there are two distinct checks, do not conflate them:
- **Per-change coherence recheck (continuous, cheap):** the deterministic gate + one
  whole-artifact reader pass after every change. This catches cross-surface contradictions
  fast; it is NOT the promotion authority.
- **Promotion gate (before a plan becomes `current`):** the FULL `promote_gate` authority set
  — codex / deterministic gate / fund_manager / whole-artifact reader / rederivation —
  fail-closed, **identical for steady-state promotion and for migration's baseline admission.**
  codex's re-derivation runs scoped to the **changed derived-value nodes** (net worth,
  US-situs, NVDA weight, FI), so it is targeted rather than a full re-review, but it is NEVER
  dropped from promotion. A plan is promotable ONLY when no node carries an open
  hard/coherence `status_flag` AND every promote_gate authority clears. Hash-validity alone
  never authorizes publication.

Analysts re-run **only** when a change targets an input they own and that input is stale —
not all 11 from zero (Layer-3 follow-on; see Phasing).

### Layer 4 — migration / coexistence

- **Hydrate the graph from the CURRENT plan** (the accepted plan / latest draft) as the base:
  derived-value nodes from the resolver manifest, surface nodes from `sections_json`, edges
  from recipes + citations + declared predicates.
- **Migration must VERIFY, not just reproduce (codex Theme D).** A round-trip proves the graph
  reproduces the existing surfaces — it does NOT prove they are correct. So hydration runs the
  **same promotion gate** (the full `promote_gate` authority set — gate / reader / codex /
  fund_manager / rederivation; see Layer 3) once on the hydrated graph and only admits a
  **clean** baseline; any uncited claim, bad locator, or prose-only reconciliation surfaces as
  an open flag to fix before the graph is trusted. Migration and steady-state share ONE
  verification surface — neither can promote what the other would reject.
- **Keep the from-scratch synthesizer** as (a) the cold-start path (new user, no plan) and
  (b) an explicit "full rebuild" escape hatch. **Steady state = incremental.**

## Data model

- `plan_nodes`: `id`, `plan_id`, `node_key`, `kind` (input|derived|surface), `value_json` /
  `content`, `input_hash` (inbound values + membership + compute-provenance versions),
  `status_validity` (valid|stale), `status_flag` (none|flagged) — **orthogonal, not one
  enum** (codex MINOR) — `provenance_json`, `owner`.
- Edges: derived at load from the recipe registry (named keys **+ set-query edges**, derived)
  + stored citations **+ declared structural predicates** (surfaces); materialized in
  `plan_edges` for query/audit.
- `change_requests`: `id`, `plan_id`, `target_node_key`, `author`, `kind`, `payload_json`,
  `rationale`, `status` (proposed|in_dialogue|escalated_arbiter|escalated_user|A_conceded|
  B_conceded|arbiter_ruled|superseded), `round_count`, `adjudicated_by`, `terminal_reason`,
  timestamps.

## How a steady-state run works

1. Collect pending ChangeRequests (user inbox + agents that ran this cycle).
2. Adjudicate each via the negotiation ladder (peer n=3 → arbiter → user); fail-closed hard
   nodes. **Conflict semantics (codex MED):** if two accepted requests touch the same input or
   set mutually-inconsistent policies, they do NOT both apply — the overlap is itself a
   conflict resolved by the ladder (arbiter picks/merges) BEFORE anything is applied.
3. Apply the conflict-free accepted set; mark dirty.
4. Propagate (Layer 3) over the EXPANDED graph: recompute the dirty+new closure; re-render
   dependent surfaces; reuse truly-independent surfaces. Batch overlapping closures → recompute
   the union once (no thrash).
5. Re-verify: blast-radius for expensive checks, **global** for cross-surface coherence.
6. Publish only if no open hard/coherence flag (the `promote_gate`). Converges by construction
   — unaffected content cannot reshuffle.

## Testing

- **Graph correctness:** recompute is deterministic; invalidation closure is exact INCLUDING
  set/membership edges (adding a tax lot invalidates every derivation over "all lots") and
  compute-version (bumping a recipe/template version invalidates its nodes with inputs
  unchanged); structural change EXPANDS the graph (new holding → new nodes/edges).
- **Adjudication + ladder:** a request setting a DerivedValue is rejected; B can reject A's
  rationale; an unresolved A↔B dispute escalates at n=3 to the arbiter; the arbiter routes a
  genuine-decision to the user and an evidence-resolvable one stays internal; a hard node
  cannot be agreed away; a verdict-flipping input change is audited/contestable.
- **Propagation:** changing one input re-renders only dependent surfaces; truly-independent
  surfaces byte-identical; a shared-narrative-state change invalidates all its consumers.
- **Global coherence:** a change that makes a changed surface contradict an UNCHANGED one is
  caught by the whole-artifact recheck (not missed by blast-radius scoping).
- **Publish gate:** a plan with any open hard/coherence flag is not promotable.
- **Migration:** hydration reproduces surfaces AND a defective imported plan yields open flags
  (not silent validity).

## Risks / edge cases (codex-review-hardened)

- **Dependency completeness — THE central risk (codex Theme A).** Edges that are only named
  scalar keys + citations miss set-membership, thresholds, absences, and compute-version
  dependencies → stale-but-valid nodes (the exact failure we exist to prevent). Mitigations,
  all above: **set-query edges** (membership-hash), **declared structural predicates** on
  surfaces, **compute-provenance in `input_hash`**, **graph expansion** on structural change.
  Residual risk: a developer adds a derivation/surface and forgets to declare a set/predicate
  edge. Guard: a **lint** that every derivation's inputs and every surface's asserted
  sets/thresholds are declared (CI), + the verification pass below as the runtime backstop.
- **Qualitative claims aren't machine-traceable like numbers (codex Theme A/B).**
  `HEADLINE_NUMERIC_SOURCE` does NOT generalize to "largest risk" / "nothing material changed."
  So the fail-closed boundary requires surfaces to be **structured into typed claims** (each
  claim carries its citations OR a declared predicate); an unstructured free assertion with
  neither is the block condition — this is buildable because the synthesizer already emits
  structured sections (`sections_json`), we tighten the per-claim requirement.
- **Cross-surface coherence is GLOBAL (codex Theme C).** A changed surface can contradict an
  unchanged one; that edge isn't a citation. Mitigation: coherence is rechecked whole-artifact
  every change (cheap gate + one reader pass), never blast-radius-only. Shared narrative state
  is its own node.
- **Fail-closed has no laundering path (codex Theme B).** Hard nodes recompute from inputs;
  premise/policy changes are themselves adjudicated + audited + contestable by the owner/arbiter;
  a surface prose patch is allowed only after the underlying derivation is verified consistent;
  publication is blocked while any hard/coherence flag is open.
- **Migration verifies, not just reproduces (codex Theme D).** Hydration runs full verification
  once and admits only a clean baseline; old defects surface as flags, not inherited validity.
- **Prose surfaces are not pure functions.** Surgical editor converges (span-local, no new
  numbers); pure-render surfaces avoid the LLM entirely.
- **Cycles.** DAG required; detect at hydration and fail loud.

## Phasing (each its own plan)

1. **Graph + propagation core** (this spec's heart): node model, hybrid edges, deterministic
   recompute, exact invalidation, migration/hydration, blast-radius re-verify. Surfaces:
   start with deterministic-render surfaces + the surgical editor for prose.
2. **Change/adjudication substrate**: ChangeRequest table, ownership map, fail-closed
   authority clearance, FM/reader/codex emit change-requests instead of whole-plan verdicts.
3. **Scoped agent re-runs**: an analyst re-runs only when its owned input is stale.

Non-goals for sub-project 1: re-running analysts incrementally (phase 3); a UI for authoring
change-requests (the user's existing intake + the agents are the authors initially).
