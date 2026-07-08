# Corrective (critique-fed) re-synthesis — design

**Status: DESIGN — approved-pending-Ariel; do not run the pending re-synthesis until this ships.**
Author: fleet, 2026-07-08. Requested by Ariel: *"If I run re-synthesis, will it feed in the
Critique? It should not start from zero — it will probably make the same mistakes. Feed in the
Critique and make corrections — harder design, but cheaper run and a more correct one."*

## 1. Problem

A re-synthesis today starts from zero: `run_synthesis()` sees the baseline, the prior current
plan (as reference prose), and free-text `guidance`. The weekly critique's findings and the
reconcile loop's outcomes (`critique_reconcile.py`) never reach it. So a re-synthesis triggered
BY critique findings will plausibly reproduce the same defects, fail the same gates, and burn
another full run. The pending run must clear 9 aggregated findings (proposal
`critique_resynth:ariel`) and apply the adjudicated glide schedule (proposal 49) — exactly the
case where from-zero regeneration is wasteful and risky.

This extends the standing **edit-don't-rebuild** doctrine (incremental plan graph, SDD §1.7)
from refinement up to synthesis itself.

## 2. Design overview

Three parts: (A) a corrective-context builder that turns critique + reconcile + accepted
adjudications into STRUCTURED corrections; (B) auto-feeding it into every synthesis run +
a cheaper corrective run tier; (C) a corrections-landed verification gate after the run.

### A. Corrective context builder — `argosy/services/corrective_context.py`

`build_corrective_context(session, user_id) -> CorrectiveContext | None`

Sources (all already persisted; no new state):
1. **Latest `plan_critiques` row** for the user's current plan — findings + the embedded
   `reconcile` payload (per-finding status: fixed / escalated / disputed-…). Only findings
   whose reconcile status is `escalated` / `disputed-upheld` / `unresolved` become
   corrections — `fixed` and `disputed-withdrawn` are already closed and MUST NOT be re-fed
   (re-feeding a withdrawn finding would re-litigate a settled dispute).
2. **Open `replan_full` proposal** (`dedup_key = critique_resynth:{user_id}`) — its payload
   carries the aggregated findings verbatim; used to cross-check (1) and to know which
   proposal to close on promote.
3. **Accepted-but-unapplied adjudication proposals** — fleet verdicts Ariel confirmed whose
   application requires a synthesis (today: proposal 49, the glide-schedule adjudication).
   Selector: `status='accepted'` + `execution_state='proposed'` + kind in a small allowlist
   (`update_plan_assumption`, `replan_full`-attached verdicts). These become **directives**,
   not findings: apply verbatim.
4. **Derived facts** (`derived_facts.build_derived_facts`) — already prepended as LOCKED
   DERIVED FACTS; the builder joins each finding to the derived fact covering its surface
   (by `plan_item_ref` token match, same lenient matcher as `findings_match`) so every
   correction carries its canonical value + derivation, not just "this is wrong".

Rendered shape (one block, deterministic order, numbered):

```
CORRECTIVE RE-SYNTHESIS — this run exists to CLEAR the corrections below while EDITING the
prior plan. You are NOT drafting from zero. Preserve every plan element NOT implicated by a
correction; the prior current plan (v67, #<id>) is the base document.

CORRECTIONS (each must be resolved; the post-run verifier checks each one):
[1] RED · <topic> · surface: <plan_item_ref>
    wrong: <finding summary + evidence>
    canonical: <value + derivation source (derived-fact key / raw rows)>
    required: <what the corrected surface must state>
...
DIRECTIVES (adjudicated verdicts — apply verbatim, do not re-decide):
[D1] proposal #49 — NVDA glide schedule 2026/2027/2028 = 4,136 / 5,094 / 592 sh
     (fast-on-eligible-core, §102-feasible). Replace the 12-month glide.
```

The `CorrectiveContext` object keeps the structured list (finding refs, canonical values,
proposal ids) alongside the rendered block — the verifier (part C) and the promote hook
consume the structured form, never re-parse prose.

### B. Feeding the run

1. **Auto-attach, never opt-in.** `run_synthesis()` calls `build_corrective_context()` at
   start (same spot as the derived-facts prepend). If corrections exist they are prepended
   to `guidance` — every phase already receives `guidance` as `user_directive`, so analysts,
   debates, synthesizer, risk, and FM all see the corrections with zero new plumbing. Nobody
   has to remember to feed the critique; a from-zero re-synthesis while findings are open
   becomes impossible by construction. Flag: `ARGOSY_CORRECTIVE_SYNTHESIS` (default ON),
   fail-soft like derived facts (a builder crash logs + degrades to today's behavior; the
   part-C gate is the backstop).
2. **Base-document emphasis in phase 3.** The synthesizer already receives
   `prior_current_md`. In corrective mode the phase-3 prompt frames it as the BASE ARTIFACT
   being edited ("carry unimplicated sections forward; change only what a correction or
   directive requires") rather than background reference. This is the edit-with-corrections
   semantics — prompt-level, no schema change, mirroring how `_codex_numeric_reconcile_guidance`
   and `_reader_coherence_reconcile_guidance` already steer re-runs.
3. **Cheaper run tier — corrective = phases 3–5.** Precedent: the medium plan-amendment
   worker already runs phase 3 only; `resume_from_phase` + `_load_completed_phase_outputs`
   already reload persisted phase outputs. Corrective mode reuses the most recent completed
   run's phase 1–2 outputs (analysts + debates) when they are fresh (≤ `ARGOSY_CORRECTIVE_PHASE_REUSE_DAYS`,
   default 14) — needs one small extension: `reuse_phases_from_run_id` (today reuse is
   restricted to the SAME run id). Phases 4–5 (risk, FM) ALWAYS run fresh — they are the
   blind gates and are never reused (adversarial-review-must-re-derive-blind doctrine).
   Stale or missing phase 1–2 outputs → full 5-phase run with corrections attached; the
   run never silently degrades (log + `synthesis_inputs_json.corrective.reused_phases`).
   Correction classes that implicate phase-1 inputs (e.g. `refresh_snapshot`-routed findings)
   force the full tier.

### C. Corrections-landed verification (the gate)

After phase 5, before the draft lands:
1. **Deterministic floor** — for each correction with a canonical VALUE, check the value
   appears at (and the wrong value is absent from) the correction's surface in the rendered
   draft + `target_allocation_json` + structured horizon JSON. Pure lookup/arithmetic — this
   is the inviolable-arithmetic floor, not a judgment gate.
2. **Judgment pass** — the whole-artifact reader runs with the corrections list as its
   directive: "verify each correction is genuinely resolved in substance, not cosmetically
   absorbed." (Reader + directive plumbing already exists.)
3. **Outcome**: all corrections landed → draft proceeds to the normal promote path; on
   `/accept` the fed proposals flip `status='executed'` and the critique panel shows
   "cleared by draft #N". Any correction NOT landed → the draft still persists (never
   discard paid work) but carries `corrective_unresolved` in `synthesis_inputs_json`; the
   accept route 422s on it exactly like an FM rejection (explicit override possible), and
   ONE aggregated inbox row surfaces what didn't land. Bounded: no auto re-run loop —
   same cost discipline as `critique_reconcile`.

## 3. What this deliberately does NOT do

- No new agent. The corrections are inputs to the existing team; the reader/FM stay the
  blind judges. Determinism only VERIFIES values landed — it never decides what's correct.
- No unbounded convergence loop — one corrective run, one verification, then a human-visible
  outcome (promote or ONE inbox row).
- No hand-patching of graph-authored surfaces — that path stays `requires_resynthesis`;
  this design is that re-synthesis done right.

## 4. Touchpoints

| Change | File |
|---|---|
| NEW `CorrectiveContext` + builder + renderer | `argosy/services/corrective_context.py` |
| Auto-attach at run start; corrective tier select; `reuse_phases_from_run_id`; base-artifact framing in phase 3; part-C gate call | `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` |
| Reader directive for corrections verification | `argosy/orchestrator/flows/plan_synthesis/whole_artifact_reader.py` (existing directive param) |
| Accept route: 422 on `corrective_unresolved`; flip fed proposals to executed on promote | `argosy/api/routes/plan.py` |
| Deterministic corrections-landed check | new `argosy/quality/corrections_check.py` |

## 5. Test plan

- Builder unit tests: reconcile-status filtering (fixed/withdrawn excluded), adjudication
  selection, derived-fact join, deterministic rendering.
- Orchestrator: corrective tier chosen iff fresh phase-1/2 outputs + no snapshot-class
  correction; degrade-to-full logged; flag OFF = today's behavior byte-identical.
- Gate: synthetic draft with a correction landed / not-landed / cosmetically-absorbed
  (value present but surface contradicts) → proceed / 422 / 422.
- Accept path: promote flips proposals; unresolved blocks without override.
- Live: THE pending re-synthesis (9 findings + proposal 49) is the acceptance run —
  verify-run it afterwards.
