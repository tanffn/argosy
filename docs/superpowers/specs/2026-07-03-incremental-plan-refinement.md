# Incremental Plan Refinement — Design Spec (v3, codex-reviewed + blind-code-verified)

**Status:** design, GO-WITH-CHANGES from codex (all must-fixes folded in), then **blind-verified against raw code** (a reviewer given only the code + a refute-mandate — because codex reviewed this *spec*, not the codebase, and rubber-stamped two assumptions; see [[feedback_adversarial_review_must_re_derive_blind]]). Two corrections from that verification are marked **[BLIND-FIX]** below. Ready to turn into a task-by-task implementation plan.
**Goal:** refine a plan by editing only what a change touches — never redeploy the full agent fleet, never rewrite the whole plan — and be **smart**: predict each change's blast radius on a sandbox graph and choose *scoped edit* vs *bounded re-derivation* vs *full rebuild* by **owner/topology/policy-axis**, not by node counts.

**Acceptance is FUNCTIONAL, not test-suite-green.** A 3-hour green suite that yields mediocre-but-"correct-for-the-fixture" output is a failure. Every capability is accepted by *running a real refinement, reading proof artifacts (graph diff, agent-invocation trace, invariant report) to confirm what actually recomputed, and judging output quality* — unit tests are scaffolding, not the gate.

---

## 1. Grounding — reuse vs build (verified 2026-07-03)

**Reuse (solid, do not rebuild):** `derivation_graph.py` (exact transitive-dependents recompute, version-stamped, skips valid nodes); `change_adjudication.py` (`ChangeRequest` + fail-closed routing); `negotiation_ladder.py` + `plan_node_owner.py` + `ladder_participants.py` (owner/arbiter, built + unit-tested); `graph_store.py` + `PropagationEvent` (persist + replay); existing from-scratch synthesis (the T2 path).

**Build (product + policy layer — ~0% today):** the refinement API; the **blast-radius sizer + tier classifier**; **allocation/glide/thesis graph coverage**; prod wiring for owner participants; money-safety invariants; staged-draft/promote/rollback; the functional harness + structured proof logs.

**Key correction to the "cutover" story:** `/accept` today calls `run_incremental_cycle(change_requests=[])` — a *coherence gate* only; plan content is still authored by full synthesis. This spec adds the *authoring-by-refinement* path the cutover never built.

Ties to **SDD §1.7 (plan vs execution)**: refinements land on *plan* (strategic) nodes. A cyclical/execution concern is **not** a plan refinement — it is rejected or routed at intake (§6).

---

## 2. Architecture

A refinement is a **`ChangeRequest` on a graph node**. The sizer **simulates apply + recompute on a cloned (sandbox) graph** to produce a *predicted diff*, the classifier picks a tier from owner/topology/policy metadata, and the tier is executed with the minimum machinery it needs — then **money-safety invariants are re-checked after every tier (including T0)**; any breach escalates, never partial-publishes.

```
refine(changes, base_version)
  → sandbox = clone(promoted_graph)
  → predicted_diff = sandbox.apply(changes).recompute()      # hard-verdict flips known here
  → tier, reason = classify(blast_radius(predicted_diff))     # batch-classified, not per-change
  → execute:  T0 recompute-only | T1 owner(s)+recompute | T2 escalate→synthesis
  → invariants = check_money_safety(sandbox)                  # after ANY tier
       if breach: escalate to T2 or staged-failure (never partial publish)
  → stage draft (version N+1, base=N)  → explicit promote (optimistic concurrency)
```

**Principle: the number of agents that run = f(blast radius), never a constant.** Only owners whose nodes are dirtied re-run; the full fleet runs *only* on T2.

### 2.1 File structure
- Create `argosy/quality/blast_radius.py` — sandbox sizer + tier classifier (pure; the heart).
- Create `argosy/quality/plan_node_meta.py` — the node metadata schema (§4) + owner-coverage validation.
- Create `argosy/quality/allocation_graph.py` — allocation nodes/recipes/edges (§5-graph).
- Create `argosy/quality/plan_invariants.py` — money-safety invariant checks (§3.3).
- Modify `argosy/orchestrator/flows/incremental_plan.py` — tiered execution + escalation + staged-draft + proof-event emission.
- Modify `argosy/orchestrator/flows/ladder_participants.py` — prod factory for `RealLadderParticipants`.
- Create `argosy/api/routes/plan_refine.py` — `POST /api/plan/refine`, `POST /api/plan/refine/promote`, `POST /api/plan/refine/rollback`.
- Modify `argosy/state/graph_store.py` — persist refinement proof fields + staged drafts + version lineage.
- Create `scripts/refine_smoke.py` — the FUNCTIONAL harness (real refinement on live data → prints tier, diff, owner trace, invariants).

---

## 3. The sizer + classifier (core; codex's topology/policy-axis design)

### 3.1 Sizing (on a sandbox, not the promoted graph)
`size_blast_radius` **clones the graph, applies the change(s), recomputes**, and reads the predicted diff — because `hard_verdicts_flipped` and cross-owner effects can only be known by simulation, not static edge inspection.

```python
@dataclass(frozen=True)
class BlastRadius:
    dirtied_keys: tuple[str, ...]
    owner_domains: frozenset[str]
    flipped_hard_verdicts: tuple[HardVerdictFlip, ...]     # each carries a severity
    introduces_structure: bool
    structure_scope: StructureScope                        # local_owner | cross_owner | new_owner_domain
    changed_policy_axes: frozenset[PolicyAxis]
    changes_plan_identity_axis: bool
    adds_or_removes_owner_domain: bool
    adds_cross_owner_dependency: bool
    invalidates_global_invariant: bool
    missing_owner_for_changed_node: bool
    touched_rebuild_boundaries: frozenset[str]
    touches_owner_authored_surface: bool
    dirtied_boundary_fraction: float                       # fallback guardrail only
```

### 3.2 Classifier (topology/owner/policy-axis first; counts only as fallback)
```python
def classify(br, cfg) -> tuple[Tier, str]:
    # --- T2 FULL_REBUILD: no bounded owner can safely repair this ---
    if br.missing_owner_for_changed_node:      return T2, "changed node has no bounded owner"
    if br.changes_plan_identity_axis:          return T2, "changes plan identity / core policy axis"
    if br.adds_or_removes_owner_domain:        return T2, "changes owner-domain structure"
    if br.adds_cross_owner_dependency:         return T2, "introduces cross-owner dependency"
    if br.invalidates_global_invariant:        return T2, "invalidates a global plan invariant"
    if any(f.severity == "plan_basis" for f in br.flipped_hard_verdicts):
                                               return T2, "flips a plan-basis hard verdict"
    if len(br.touched_rebuild_boundaries) > 1 and br.changed_policy_axes:
                                               return T2, "policy change crosses rebuild boundaries"
    # --- T1 BOUNDED_REDERIVE: bounded owner authoring/reconciliation ---
    if br.introduces_structure:                return T1, "local structure change → owner repair"
    if len(br.owner_domains) > 1:              return T1, "bounded multi-owner change"
    if br.flipped_hard_verdicts:              return T1, "localized hard-verdict flip"
    if br.touches_owner_authored_surface:      return T1, "owner-authored surface → reconciliation"
    if br.dirtied_boundary_fraction > cfg.max_scoped_boundary_fraction:
                                               return T1, "large localized blast radius"
    # --- T0 SCOPED_EDIT: deterministic recompute/render only ---
    return T0, "deterministic localized edit"
```

Node counts (`0.5*graph_size`, `hard_verdicts_flipped>=2`) are **removed as primary signals** — kept only as `dirtied_boundary_fraction` anomaly guardrails. **The SWR-anchor contradiction is resolved:** the SWR anchor is a *refinable knob* on the `withdrawal` policy axis, NOT a plan-identity axis — so "SWR 3.0→3.25" is T0/T1 (localized FI recompute), while "switch to capital-preservation posture" flips a *plan-identity* axis → T2.

### 3.3 Money-safety invariants (checked after EVERY tier, incl. T0)
`plan_invariants.py::check(sandbox) -> InvariantReport`: allocation weights sum to 100 within tolerance; every sleeve within its band; single-name cap satisfied on a **direct+fund look-through** basis (SDD §1.7); FI-safety verdict not silently degraded below policy floor; tax/estate constraints (no new unsanctioned US-situs) hold. **Any breach ⇒ escalate to T2 or return a staged failure — never a partial publish.** A bounded owner that fails, returns invalid output, or violates an invariant escalates the same way.

---

## 4. Node metadata (drives the classifier; validated at graph build)

**[BLIND-FIX] This metadata does NOT exist on nodes today.** Verified against `derivation_graph.py`: `Node` has only `key, kind, value, inputs, recipe, compute_version, input_hash` — no owner, domain, or axis field, and there is **no policy-axis concept anywhere** in the codebase. So `plan_node_meta.py` is a from-scratch addition. The clean anchor already exists: the node `key` is a stable dotted namespace (`retirement.*`, `concentration.*`, `portfolio.*`) already used for ownership routing by `change_adjudication.OwnershipMap.owner_of` and `ladder_participants._OWNER_BY_PREFIX`. Attach owner/axis metadata **keyed off the dotted node key + `OwnershipMap`**; do not invent a parallel node identity. Build fails loud if a mutable node has no owner/axis mapping (fail-closed).

Every node carries (via the new metadata layer):
- `owner_domain` — the topic-owner that may author/repair it.
- `policy_axis` ∈ {risk, withdrawal, tax, allocation, estate, concentration, execution, prose}.
- `authoring_mode` ∈ {deterministic, owner_authored, synthesis_authored}.
- `boundary_id` — FI | allocation | tax | estate | render_only | …
- `rebuild_boundary: bool` — crossing this = a plan-basis change.
- `plan_identity_axis: bool` — true only for identity params (risk posture, tax residency, the plan's objective) — NOT for refinable knobs like the SWR anchor.
- `hard_verdict_severity` ∈ {cosmetic, localized, plan_basis} (for hard nodes).
- `structure_scope` for structural edits ∈ {local_owner, cross_owner, new_owner_domain}.

**Owner-coverage validation at graph build time:** every `owner_authored`/`synthesis_authored` node must resolve to a live owner; a node with no bounded owner forces T2 (`missing_owner_for_changed_node`). Build fails loud if metadata is missing (fail-closed).

---

## 5. Refinement entry point + safety rails

`POST /api/plan/refine`:
```
Request : { user_id, base_plan_version, changes: [ChangeRequest], reason, idempotency_key, dry_run?: bool }
Response: { tier, reason, blast_radius, predicted_diff:[{key,before,after}], rerendered_surfaces:[...],
            invariant_report, escalated_to_rebuild, staged_draft_version | null,
            result_type: STAGED | ROUTED_TO_EXECUTION | REJECTED_EXECUTION_CONCERN | REJECTED_UNAUTHORIZED | CONFLICT }
```
- **Batch-classified:** a multi-change request is sized + classified as ONE batch (not per-change), so interacting changes get the correct tier.
- **`dry_run`** previews (size+classify+sandbox diff), persists nothing.
- **Staged draft + explicit promote** (codex must-fix): a real refinement produces a *staged draft* version (base=N → N+1) recording the diff; `POST /api/plan/refine/promote` commits it; `POST /api/plan/refine/rollback` reverts to a version pointer. `dry_run` alone is NOT sufficient to mutate a promoted plan.
- **Optimistic concurrency:** `base_plan_version` required; if the promoted plan advanced, return `CONFLICT` (rebase or re-preview) — never blind-overwrite.
- **Authorization + node allowlist:** only allowlisted nodes are externally mutable; others → `REJECTED_UNAUTHORIZED`.
- **Idempotency key:** dedupes retried API calls.
- **Execution concerns rejected/routed at intake:** a cyclical/deployment change returns `ROUTED_TO_EXECUTION` (with the generated execution task) or `REJECTED_EXECUTION_CONCERN` — it never pollutes the plan graph.
- **Audit log:** user/agent identity, payload, tier + reason, diff, invariant report, publish decision — every refinement.
- A `PlanRevisitFlag` (SDD §1.7) is converted to a `ChangeRequest` and enters this path.

**Allocation graph coverage** (`allocation_graph.py`) so allocation refinements are scoped: `sleeve_target::<id>` (INPUT, axis=allocation), `sleeve_band::<id>` (INPUT), `allocation_normalized` (DERIVED, owner=allocation, renormalize to 100), `glide_waypoint::<n>`/`glide_curve`, `single_name_cap` (INPUT, look-through, `rebuild_boundary=True`), and `sleeve_thesis::<id>` (SURFACE, `authoring_mode=owner_authored`). **Thesis nodes render/reconcile from structured facts + bounded owner repair — they must NOT invent strategic rationale** (that stays synthesis; see §6 line).

**[BLIND-FIX] Two verified realities about allocation this must respect:**
1. **Allocation targets are deterministic, but only from *hardcoded coarse sub-sleeve ratios*** (`allocation_plan.py:105-333`: static `ratio=` seeds renormalized in `_renormalise`) — "reproducible from baked policy seeds," NOT dynamically re-derived from live look-through. This is exactly the coarse water-fill the fleet-authors pivot exists to replace ([[feedback_fleet_authors_determinism_verifies]]). So model `sleeve_target::<id>` as an **INPUT node holding the authored/current target value** — the graph *refines* that value (a supplied change, or an owner/synthesis re-author). **Do NOT wire the coarse ratio recipe as a DERIVED node** — that would enshrine the water-fill as if it were derived truth. `allocation_normalized` may be DERIVED (pure renormalization to 100), but the underlying sleeve targets are authored inputs, not recomputed from ratios.
2. **The owner agent JUDGES, it does not AUTHOR figures.** `PlanNodeOwnerAgent` returns ACCEPT/REJECT/UNRESOLVED on a *supplied* value; `owner_routed_reconcile` patches prose or *surfaces* a figure change, deferring genuine figure authoring to full re-synth. So T1 splits precisely: **(a) change SUPPLIES the value** ("set growth to 8") → deterministic renormalize + owner *votes* + arbiter → true T1; **(b) the value must be GENERATED from scratch** (no supplied number, needs judgment about which sleeves absorb the delta) → there is no scoped figure-authoring agent today, so this escalates to **T2** until such an owner-authoring capability is built. The classifier must detect "unsupplied figure change" and route it to T2, not pretend a scoped author exists.

---

## 6. The graph-refines vs synthesis-authors line (codex)
**Graph refines:** existing structured facts; numeric targets/bands/constraints/glide; deterministic normalization + validation; local prose patches tied to changed facts; owner-bounded reconciliation *inside an existing section*.
**Synthesis authors (T2):** new strategic rationale; overall risk posture; household objective tradeoffs; whether a sleeve should exist *for a new strategic reason*; cross-domain policy decisions; the plan's narrative architecture.
So "growth 13.2→8 and reword that thesis line" = graph/owner refinement; "adopt an inflation-resilience strategy and decide whether real assets belong in policy" = synthesis (or a T1 owner *proposal* + explicit promote).

---

## 7. Functional acceptance (the real gate) — with proof artifacts

For each scenario: run on live data via `scripts/refine_smoke.py`, read the **proof artifacts**, confirm tier + scope, and **judge output quality myself**. Required artifacts per run (codex must-fix): (a) before/after **graph diff**; (b) promoted-version + staged-draft IDs; (c) **agent-invocation trace** proving which owners ran and which did NOT; (d) surface **byte-diff grouped by blast radius**; (e) **invariant report** (alloc sums, bands, look-through cap, FI safety, tax); replus (f) **replay check** (same staged refinement from same base → same tier + same diff) and (g) **rollback check** for promoted refinements.

Scenarios:
1. **Scalar knob — expect T0/T1.** SWR anchor 3.0→3.25. Trace: only FI subtree recomputed, `owners_run ⊆ {fi}`, allocation/tax untouched; invariants hold. Judge: FI verdict + retire-age move coherently.
2. **Sleeve tweak — expect T1, one owner.** Growth 13.2→8, real-assets 2→5. Trace: only allocation owner ran; `allocation_normalized`==100; drift + deploy-band surfaces updated; FI/tax/net-worth untouched; NO full fleet. Judge: sensible weights + re-rendered theses.
3. **Structural add — expect T1 (local_owner).** Add a real-assets sleeve. Trace: new nodes/edges within allocation owner; renormalized; no rebuild. Judge: coherent sleeve + rest unchanged.
4. **Fundamental — expect T2 with reason.** Flip to capital-preservation posture (plan-identity axis). Trace: `escalated_to_rebuild=true`, reason cites the identity axis; synthesis ran. Judge: whole plan re-derives coherently.
5. **Non-divergence + negatives.** Post 1–3, every out-of-blast-radius surface byte-identical. Negative cases: ambiguous target, unauthorized node (`REJECTED_UNAUTHORIZED`), execution-only request (`ROUTED_TO_EXECUTION`), unsupported structural edit (→T2), missing owner (→T2), stale `base_plan_version` (`CONFLICT`).

---

## 8. Testing philosophy (explicit)
- **TDD (unit)** on the pure functions — `blast_radius.size/classify`, `plan_invariants.check`, `allocation_graph` recipes — table-driven. Fast, worth it.
- **NEVER a real `claude.exe` in the suite** — inject owner/ladder participants as fakes (autouse guard already stubs the alternatives phase).
- **The acceptance gate is functional** (§7): tier mis-classification or a schema-valid-but-mediocre plan is a FAIL even with a green suite. This is the "correct-for-the-test ≠ good" guard.

---

## 9. Non-goals (do not build)
- **No regime-chasing plan owner** (SDD §1.7).
- **No re-authoring the whole plan per tweak** (the bug this fixes).
- **No new synthesis engine** — T2 reuses the existing synthesis path; this spec only decides *when* to invoke it.
- **The graph does not become the plan author** — it refines facts + bounded surface fragments, never invents investment philosophy (§6).

---

## 10. Open questions — resolved by codex review
1. Thresholds → **topology/owner/policy-axis primary; counts as guardrails only** (§3.2). ✔
2. `introduces_structure` → **never T0** unless render-only; sleeve add ≥ T1; cross-owner/new-domain → T2. ✔
3. `dry_run` insufficient → **staged draft + explicit promote + rollback + optimistic concurrency** (§5). ✔
4. Graph-as-author scope creep → **the §6 line** (facts + bounded fragments, not strategic authorship). ✔
5. Execution concerns → **typed `ROUTED_TO_EXECUTION` / `REJECTED_EXECUTION_CONCERN` at intake** (§5). ✔

**Highest-leverage first build:** `blast_radius.py` (sandbox sizer + classifier) + `plan_node_meta.py` + `plan_invariants.py` — the decision core — proven against the `refine_smoke.py` scenarios in dry-run before any promotion path is wired. Everything else (API, allocation graph, staged-draft/promote) builds on that decision core.
