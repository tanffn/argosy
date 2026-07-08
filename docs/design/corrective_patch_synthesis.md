# Corrective patch-synthesis (edit-don't-rebuild for phase 3) — design

**Status: DESIGN — sequel to `docs/design/corrective_resynthesis.md` (shipped 2026-07-08).**
Author: fleet, 2026-07-08. Extends the edit-don't-rebuild doctrine (SDD §1.7, incremental plan graph, `ARGOSY_INCREMENTAL_PLAN` default ON) the last mile: from "feed the corrections into a full regeneration" to "patch the prior draft's structured artifact."

## 1. Problem

The shipped corrective tier reuses phases 1–2 and frames the prior draft as the base document, but phase 3 (`argosy/orchestrator/flows/plan_synthesis/orchestrator.py::_run_phase_3_synthesizer` → `argosy/agents/plan_synthesizer.py`) still asks Opus to regenerate the ENTIRE `PlanSynthesisOutput` — three `HorizonSection`s plus up to 18 evidence-bearing `Section`s, ~70k chars — even when the required change is one line (live case 2026-07-08: restate one endpoint number pair). Two costs:

1. **Time.** Full-artifact Opus generation dominates each corrective pass (~20–25 min observed for phase 3 alone).
2. **Correctness.** Every full rewrite is a fresh roll of the dice on every unimplicated surface. Live case: a rewrite whose task was 9 unrelated corrections introduced a brand-new §102 vest-sale incoherence (caught by the FM on run 141). Regeneration is not a neutral operation; the safest edit is the one that physically cannot touch what it wasn't asked to touch.

The system already knows how to think about this — just not at synthesis:

- **The living-plan derivation graph** (`argosy/quality/derivation_graph.py`, `blast_radius.py`, `refinement.py`) sizes a change's blast radius on a sandbox clone and classifies SCOPED_EDIT / BOUNDED_REDERIVE / FULL_REBUILD, with a mandatory invariant override (`run_refinement`, consumed by `POST /api/plan/refine`).
- **The artifact is already sliceable.** `_rewrite_output_parallel` rewrites the four prose slices — long / medium / short / sections — as independent calls and merges; `_force_preserve_structured_fields` proves we can deterministically byte-restore protected subtrees onto a model's output.
- **Corrections are already structured.** `argosy/services/corrective_context.py` gives each `Correction` a `plan_item_ref` (item-id addressing like `medium.targets.nvda`), `canonical_facts`, and `wrong_values`; `argosy/quality/corrections_check.py` already does deterministic whole-artifact value presence/absence sweeps.

What's missing is the bridge: a blast-radius-style classifier over the ARTIFACT (not the graph — horizon prose is famously refinement-unreachable, per `critique_reconcile.py`), and a patch mode for the synthesizer.

## 2. Design overview

Three parts: (A) a pure patch-reachability classifier that maps each correction/directive to implicated slices and decides patch-vs-regenerate; (B) a patch-mode phase 3 — per-slice Opus calls that emit edited slices, deterministically merged with item-level force-preserve and provenance; (C) unchanged full-artifact verification, with one bounded escalation to full regeneration when the patch demonstrably under-scoped.

### 2.A Patch-unit granularity (the decision)

**Call/merge unit: the SLICE** — one `HorizonSection` (`long` / `medium` / `short`) or one `Section` (by `(section_id, horizon)`). **Preserve/provenance unit: the ITEM** — one `SynthTarget` / `Theme` / `Action` / `Delta` by `item_id`, or one `Section`.

Why not finer (targets row)? A changed target is almost never row-local inside its slice: the horizon's `posture`/`rationale` prose restates it, a `Delta` entry must be updated, and evidence `extract`s must contain the new value literally (the EvidencePerSection contract). Asking the model to emit a lone row and splicing it in would produce exactly the intra-slice incoherence the gates exist to catch. Why not coarser (whole artifact)? That is today's problem. The slice is the smallest unit that (a) pydantic-validates standalone against an existing schema, (b) is provably independent (the rewriter already treats slices as prose-independent), and (c) matches the corrections' addressing scheme (`<horizon>.<kind>.<slug>` → horizon slice; `section:<id>` → section).

The item is still the honesty unit: within a returned slice, every item whose `item_id` is NOT implicated by any correction is **byte-restored from the prior slice** (the `_force_preserve_structured_fields` pattern, keyed by `item_id` / `(section_id, horizon)`). A patch call physically cannot perturb an unimplicated target — the merge throws its version away. Only slice-level prose (`posture`, `rationale`, `body_md`) and implicated items accept model output.

### 2.B Part A — patch-reachability classifier: new `argosy/quality/patch_reachability.py`

Pure / deterministic, no DB, no LLM — same doctrine as `blast_radius.py`, with strict top-down precedence (FULL first). Input: the `CorrectiveContext` (structured corrections + directives) plus the prior draft's `PlanSynthesisOutput`. Output: per-correction `(scope, implicated_slices, implicated_item_ids)` and an overall verdict `PATCH | FULL_RESYNTH`.

Per-correction FULL triggers (any one fires → the whole run takes the shipped full corrective path; no mixed mode, because a full regeneration subsumes every patch):

1. **Unaddressable surface** — `plan_item_ref` resolves to no item/section in the prior draft (lenient token match, same matcher spirit as `findings_match`).
2. **No concrete edit** — the correction carries neither `canonical_facts` nor `wrong_values` nor a verbatim directive detail. Substance-only findings ("the narrative conflates X and Y") are cross-cutting judgment work; regeneration is the right tool.
3. **Cross-cutting occurrence spread** — the deterministic occurrence pre-scan (reuse `corrections_check.value_variants` + `_present`) finds the wrong/stale values in **more than 2 of the 4 slices**, or the union of implicated slices across all corrections exceeds 2 of 4. If most of the artifact is implicated, patching is regeneration with extra steps.
4. **Status-class flip** — the correction requires flipping a horizon's `status` (e.g. `no_change` → `major_revision`) or adding/removing an item class (a new section, a removed target with lineage obligations beyond a `removed` Delta).
5. **Snapshot-class correction** — already `forces_full_tier` in `CorrectiveContext`; carried through unchanged.

Crucially, slice implication is **occurrence-based, not just ref-based**: every slice where any wrong/canonical value textually occurs in the prior artifact joins the implicated set, even if `plan_item_ref` points elsewhere. This closes the "value restated in another horizon's rationale" hole *before* the model runs, rather than discovering it at the gate.

Directives (adjudicated verdicts, apply-verbatim) classify the same way; a directive like proposal 49's glide schedule maps to its target items + every slice where the superseded figures occur.

### 2.C Part B — patch-mode phase 3

New orchestrator branch `_run_phase_3_patch` selected when: corrective mode is active AND `ARGOSY_CORRECTIVE_PATCH=1` (default OFF for one release, then ON) AND the classifier verdict is PATCH. Fail-soft: any exception in the patch path logs and degrades to the shipped full corrective regeneration — never a worse outcome than today.

**Prompting.** New `PlanPatchSynthesizerAgent` (`agent_role = "plan_synthesizer"` model resolution — same Opus class; a distinct agent class, not a mode flag, so the prompt stays single-purpose). One call per implicated slice, run in parallel (mirror `_rewrite_output_parallel`'s thread fan-out). Per-call input:

- the prior slice **verbatim JSON** (full fidelity — this is the base being edited);
- ONLY the corrections/directives implicating this slice, each with canonical value + derivation + wrong values ("must be absent");
- the `DERIVED HEADLINE NUMBERS` block (authoritative, as in the full prompt);
- the relevant `prior_items_index` rows for the slice (ID-stability contract carries over verbatim);
- a short excerpt budget of HARD FACTS (portfolio snapshot summary) — NOT the full analyst/debate corpus; the patch integrates supplied canonical values, it does not re-derive.

Output schema = the existing `HorizonSection` / `Section` pydantic models via `use_structured_output=True`. **Edited-slice output, not patch ops**: JSON-patch operations would need a new op schema, fragile path addressing into nested lists, and a bespoke applier; an edited slice reuses schema, validators, and the force-preserve merge we already trust. The prompt's core contract:

> You are EDITING, not drafting. Change ONLY what a correction requires: the implicated items, the Delta entries for those items, and any sentence in posture/rationale/body_md that states a corrected figure. Reproduce everything else byte-for-byte. Unimplicated items are restored from the base regardless of what you emit — deviating only wastes your own output. Update each edited item's evidence so extracts literally contain the new values.

**Deterministic merge + provenance.** Merge order per slice: start from the prior slice; splice in the model's versions of implicated items + slice prose; byte-restore all unimplicated items, `SectionEvidence` subtrees of untouched sections, and `inputs`. Record `synthesis_inputs_json.corrective.patched_surfaces`: one row per edited surface — `{slice, item_id | section_id, correction_index | directive_index, before_sha256, after_sha256}` — plus the classifier verdict + reasons. The phase transcript gets the per-slice calls like any agent phase. Unpatched-slice hashes are recorded too, proving non-perturbation affirmatively.

**Whole-artifact re-render + downstream.** After the merge, the pipeline is UNCHANGED and full-artifact:
- pydantic re-validation of the merged `PlanSynthesisOutput` (round-trip, as the resume path does);
- `_run_plan_language_rewriter` on the **patched slices only** + `_force_preserve_structured_fields` + rewriter invariants;
- `_enforce_speculation_cap`;
- markdown re-render via `render.py` (deterministic — whole-document re-render is free);
- **phases 4, 4.5, 5, the whole-artifact reader, plan-risk-kernel invariants, numeric/coherence gates, and the corrections-landed check all run fresh over the full merged artifact, blind.** They are not told a patch happened (blindness preserved); the safety architecture is byte-identical to today.

### 2.D Part C — bounded escalation

If the post-patch deterministic floor (`check_corrections_landed`) or the reader's corrective-directive pass finds a correction NOT landed — including a wrong value surviving in a slice the classifier didn't implicate — the run escalates ONCE to the shipped full corrective regeneration (the orchestrator already has exactly this re-synth fallback machinery for reader blocks; reuse it). One escalation, then the normal outcome path (promote, or 422-guarded draft + one inbox row). No convergence loop — same cost discipline as `critique_reconcile`. The escalation event is logged with the classifier's original reasons so under-scoping is diagnosable, and the classifier rule that missed gets a test.

### Expected time saving

Phase-3 full generation is ~25–30k output tokens (~20–25 min observed). A patch pass for a typical corrective case (1–3 corrections, 1–2 implicated slices) generates 1–2 slices of ~2–6k tokens each, in parallel: **phase 3 drops from ~20–25 min to ~2–4 min** plus phases 1–2 already reused. Honest remainder: phases 4/4.5/5 + reader are unchanged by design, so end-to-end a corrective pass goes roughly from ~40–50 min to ~20–25 min. The correctness gain — unimplicated surfaces byte-identical by construction — is arguably worth more than the minutes.

### Riskiest failure mode + mitigation

**Patch myopia — cross-surface staleness**: the mirror image of the live full-rewrite bug. The patched slice states the new figure while related prose elsewhere (another horizon's rationale, the sections appendix) still asserts the old one, and unlike a full rewrite there is no chance the regeneration incidentally fixes it. Mitigation is layered and mostly deterministic: (1) *pre-run*, occurrence-based slice implication scans the prior artifact for every wrong/stale value and widens the implicated set (or forces FULL when spread > 2 slices); (2) *post-run*, the corrections-check floor sweeps the WHOLE merged artifact for surviving wrong values — a hit in any slice, patched or not, fails the check; (3) the blind whole-artifact reader judges substance over the full document; (4) one bounded escalation to full regeneration when (2)/(3) fire. A stale value can therefore slip only if it is phrased without the literal figure AND the reader misses the substance — the same residual risk full regeneration has today, minus regeneration's own perturbation risk.

## 3. What this deliberately does NOT do

- **No weakening of the blind gates.** Phases 4–5, the codex second opinion, the whole-artifact reader, and the corrections-landed check remain FULL-ARTIFACT and are not told the draft was patched. Determinism decides *scope* and verifies *values*; it never judges correctness.
- **No patch for judgment-shaped corrections.** A correction without a concrete canonical/wrong value or verbatim directive routes to full regeneration by construction — the classifier is honest about reachability, exactly as `critique_reconcile` is honest that horizon prose is refinement-unreachable.
- **No JSON-patch op language, no new artifact schema.** Output is edited slices in the existing pydantic types; the merge is the existing force-preserve pattern.
- **No convergence loop.** One patch attempt, one bounded escalation to full regeneration, then a human-visible outcome.
- **No change to `/api/plan/refine`.** The living-plan graph path and this synthesis-artifact path stay separate classifiers over separate substrates (graph nodes vs. artifact slices); unifying them is future work once horizon prose is modelled in the graph.

## 4. Touchpoints

| Change | File |
|---|---|
| NEW pure patch-reachability classifier (per-correction scope, occurrence pre-scan, PATCH/FULL verdict, strict precedence) | `argosy/quality/patch_reachability.py` |
| NEW patch-mode synthesizer agent (per-slice prompt; output schema = existing `HorizonSection` / `Section`) | `argosy/agents/plan_patch_synthesizer.py` |
| `_run_phase_3_patch` branch: tier select, parallel slice calls, deterministic merge + item-level force-preserve, patched-slice-only rewriter, provenance, escalate-once wiring; flag `ARGOSY_CORRECTIVE_PATCH` | `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` |
| `Correction`/`Directive`: parsed slice/item addressing carried on the dataclass; `to_payload` extension | `argosy/services/corrective_context.py` |
| Whole-artifact stale-value sweep result feeds the escalation decision (floor already exists; add per-slice attribution to the reason) | `argosy/quality/corrections_check.py` |
| Read `synthesis_inputs_json.corrective.patched_surfaces` in the decisions dev pane (display-only) | `argosy/api/routes/plan.py` |

## 5. Test plan

- **Classifier unit tests**: each FULL precedence rule fires in isolation; occurrence pre-scan widens implication to a slice not named by `plan_item_ref`; >2-slice spread forces FULL; substance-only correction forces FULL; directive mapping (glide-schedule shape).
- **Merge tests**: unimplicated items byte-identical post-merge regardless of what the stub patch agent emits (adversarial stub that mutates everything); implicated item + its Delta + slice prose taken from model output; evidence subtrees of untouched sections restored; provenance rows carry correct hashes + correction ids.
- **Orchestrator**: patch tier chosen iff corrective + flag + PATCH verdict; degrade-to-full on classifier/agent exception logged; flag OFF = shipped corrective behavior byte-identical; escalation path runs the full re-synth exactly once when the floor reports a surviving wrong value in an unpatched slice.
- **Gates**: merged artifact passes pydantic + rewriter invariants + plan-risk-kernel; a synthetic patch that lands the value in one slice while another slice retains the wrong value → floor fails → escalation.
- **Live acceptance**: the live case class — a single endpoint number-pair restatement — run under `ARGOSY_CORRECTIVE_PATCH=1`; verify-run it; confirm unpatched-slice hashes match the prior draft and phase-3 wall-clock < 5 min.
