# Sliced full synthesis (two-stage skeleton + parallel expansion for phase 3) — design

**Status: DESIGN — third in the series after `docs/design/corrective_resynthesis.md` and `docs/design/corrective_patch_synthesis.md` (both 2026-07-08).**
Author: fleet, 2026-07-08. The predecessors made *corrective* passes cheap (reuse phases 1–2; patch concrete corrections per-slice). This doc fixes the remaining monolith: **FULL phase-3 generation** — from-scratch synthesis and judgment-shaped corrective passes that the patch classifier correctly routes to full regeneration.

## 1. Problem

`_run_phase_3_synthesizer` (`argosy/orchestrator/flows/plan_synthesis/orchestrator.py` → `argosy/agents/plan_synthesizer.py`) emits the entire ~70k-char `PlanSynthesisOutput` — three `HorizonSection`s plus up to 18 evidence-bearing `Section`s — in ONE Opus call: ~90–230k effective input tokens, 30–60k output tokens, 15–28 min per attempt (log evidence, 2026-07-08). The failure economics are brutal because **the attempt is the unit of loss**:

- **Run 149 (live):** attempt 1 hit the 900s `sdk_timeout` (16:17); attempt 2 emitted malformed JSON — `Expecting ',' delimiter` at char 9,036 (16:28); attempt 3 malformed at char 6,756 (16:40). Each failed attempt discarded ALL generation work and restarted from zero.
- The same afternoon's earlier runs show the full failure menagerie on this one call: schema-validation retries (full re-emit), malformed-JSON retries at random offsets (chars 50,940 / 44,346 / 9,036 / 6,756), a parse recovered only by scan-repair, and the timeout. A 40–60k-token generation integrates a per-token hazard over ~20 minutes; every hazard class costs the whole artifact.

The reason phase 3 is monolithic is real: targets, deltas, statuses, posture, and prose must agree **across** horizons and sections. The corrective PATCH mode dodges this by editing an existing coherent artifact; full generation has no base artifact to lean on. The design question is how to slice generation without losing that cross-slice agreement.

## 2. Design overview

**Two-stage: a small "skeleton" call that makes every cross-cutting decision, deterministically gated against the derived-numbers floor BEFORE fan-out; then parallel per-slice expansion calls that receive the skeleton verbatim as the coherence contract and emit the existing schemas; then deterministic assembly that byte-enforces the skeleton's locked fields — reusing the patch-mode merge/force-preserve machinery.**

The key insight making this safe: **everything that must agree across slices is small.** The targets table with numbers, the delta roster, per-horizon status, the section roster, speculation numbers — together ~5–8k output tokens. What is large (rationale prose, `body_md`, evidence extracts, action detail) is exactly what the rewriter already treats as slice-independent (`_rewrite_output_parallel`). Slice the large part; centralize and lock the small part.

### 2.A Stage A — the skeleton

New agent `PlanSkeletonSynthesizerAgent` (`agent_role="plan_synthesizer"` model resolution — same Opus class, `use_structured_output=True`), new compact schema `PlanSkeleton`:

- **Per horizon (×3):** `status`, `freshness_expected`, `posture_summary` (2–4 sentences — the stance, not the essay), the **full `SynthTarget` list with numbers** (targets are the cross-slice numbers table; they reuse the existing `SynthTarget` model unchanged), theme roster (`label`, `direction`), action roster (`label`, `horizon_kind`, `trigger_or_date`), speculative-candidate roster with all numeric fields (short only; cap-constrained).
- **Delta roster:** every `Delta` minus its prose (`item_id`, `item_kind`, `horizon`, `change_kind`, `summary`) — the roster IS the change contract; expansion may only fill `rationale`/`prior`/`proposed`/citations.
- **Section roster:** list of `(section_id, horizon, one_line_thesis, key_facts)` — which of the 18 canonical sections exist, where, and the 1–3 facts each must state (with values).

Inputs = exactly today's monolith prompt (hard facts, analyst reports, debate outcomes, `DERIVED HEADLINE NUMBERS`, prior-items index, user directive / corrective block). The derivation-ownership, ID-stability, and unit-discipline rules move here wholesale — **the skeleton is where numbers are decided**, so it is where those rules bind. Output ~5–8k tokens, ~2–4 min: cheap to retry, and small enough that the malformed-JSON/timeout hazard classes effectively vanish for this call.

**Skeleton gate (deterministic, pre-fan-out)** — new `argosy/quality/skeleton_gate.py`, pure like `patch_reachability.py`:

1. Every headline `SynthTarget` value matches the resolved-numbers manifest (`plan_numeric_resolver.resolve_plan_numbers`) or is `[derivation pending]` — the #1 historical reject, caught **before** 60k tokens are spent instead of after.
2. Corrective mode: every correction's canonical value present, wrong values absent (reuse `corrections_check.value_variants` / `_present`).
3. Delta `item_id`s resolve against `prior_items_index` (or are well-formed new ids); `status=no_change` ⇒ empty delta roster for that horizon.
4. Section coverage ≥ the MVP floor (≥12 of 18 canonical ids); speculation roster within the cap.

Gate failure → ONE skeleton retry with the violations fed back (the `schema_retry_attempts` pattern), then loud abort. This is the "cheap early gate" — it converts the most expensive downstream rejection class into a 3-minute retry.

### 2.B Stage B — parallel expansion

Six calls, fan-out mirroring `_rewrite_output_parallel` / `_run_phase_3_patch` (`ThreadPoolExecutor`, `as_completed`): one per `HorizonSection` (long / medium / short) + section batches grouped by horizon (3 calls; a batch is its horizon's roster entries, typically 4–6 `Section`s). New `PlanSliceSynthesizerAgent`; output schema = the **existing** `HorizonSection` / `list[Section]` pydantic models — no new artifact schema, mirroring the patch agents.

Per-call input, in this order so the shared prefix is byte-identical across all six calls (prompt-cache-shared — the siblings pay cache-read, not cache-creation, for the ~200k-token corpus):

- the full HARD FACTS corpus (portfolio snapshot, analyst reports, debate outcomes) — sections need verbatim analyst text for `Citation.extract`s, so no lossy per-slice excerpting;
- the `DERIVED HEADLINE NUMBERS` block;
- the **complete skeleton verbatim** — all three horizons' postures + the full targets/delta/section rosters, so cross-horizon prose references always have the correct figures in view;
- last (varying suffix): this slice's assignment — "expand horizon `medium`" / "write sections [(tax_plan, short), …]" — plus the prior-items index rows and corrective corrections relevant to it.

The prompt contract, mirroring the patch agent's: *you are EXPANDING a decided skeleton, not deciding. Target values, delta identities, statuses, and rosters are locked — deviating only wastes your output, because assembly restores them. Your job is rationale, posture prose, action detail/how_to/done_when, body_md, and evidence whose extracts literally contain the skeleton's values.*

**Per-slice retry + salvage.** Each slice gets its own orchestrator retry envelope (2 retries on the transient classes — `sdk_timeout`, malformed JSON, exit-1) on top of the SDK's internal retries. A dead slice never kills siblings: `as_completed` collects every result; failures are recorded per-slice. Each completed slice is persisted **immediately** as a `decision_phases` sub-checkpoint — `kind='synthesis.phase_3.skeleton'` and `kind='synthesis.phase_3.slice.<name>'` — with the skeleton's sha256 stamped in the payload. A run retry with `resume_from_phase=3` reloads the skeleton + all slices whose skeleton-hash matches and re-runs ONLY dead slices. K-of-N partial completion is never lost work again. (`_load_completed_phase_outputs` ignores non-integer kind suffixes today, so sub-checkpoints are backward-inert; a small parallel loader reads them.)

### 2.C Assembly — deterministic, lock-enforcing

New `_assemble_sliced_output`: build `PlanSynthesisOutput` from the six slice outputs, then **byte-enforce the skeleton locks** using the same splice/force-preserve pattern as `_merge_patched_output`: every `SynthTarget` numeric field, `status`, `freshness_expected`, delta structural fields, section `(section_id, horizon)` identity, and speculative-candidate numbers are restored from the skeleton regardless of what a slice emitted; only prose/evidence fields accept expansion output. Model-invented items/sections/deltas not in the roster are dropped; roster entries a slice omitted fail assembly loudly (never a silent hole). Then: whole-artifact pydantic round-trip, and provenance in `synthesis_inputs_json.sliced` — skeleton hash, per-slice hashes, retry counts, lock-restoration counts.

**Downstream is byte-for-byte today's pipeline**: the assembled output flows into `_run_plan_language_rewriter` (already slice-parallel), `_enforce_speculation_cap`, phases 4 / 4.5 / 5, the whole-artifact reader, numeric/coherence gates, and (corrective) the corrections-landed check — all full-artifact, all blind to the slicing. `render.py` re-renders deterministically. No consumer changes.

**Selection + fail-soft.** Flag `ARGOSY_SLICED_SYNTH` (**default ON** after live acceptance; kill switch `ARGOSY_SLICED_SYNTH=0`). Precedence: corrective PATCH (when its flag + verdict fire) > sliced FULL > monolith. Any stage-A/assembly exception degrades to the monolith (log + provenance note) — never a worse outcome than today. A dead slice after retries fails phase 3 as today, but with sub-checkpoints intact for resume.

### 2.D Expected benefit (honest)

| | Monolith today | Two-stage |
|---|---|---|
| Phase-3 happy path | 15–28 min, one 30–60k-token call | ~10–14 min: skeleton 2–4 + gate ~0 + slowest slice 4–8 (parallel) + assembly ~0 |
| Cost of one transient failure | full attempt: 15–28 min, ALL work lost | skeleton retry ~3 min; slice retry ~5 min; siblings salvaged |
| Run 149's day (3+ attempts) | 45–80 min phase 3 | worst observed class ≈ 15–20 min |
| Output tokens total | 30–60k | +10–20% (skeleton restates targets/rosters that slices also render) |
| Input tokens total | 1× corpus | ~6× nominal, but cache-shared prefix → marginal cost ≈ 1× creation + 5× cache-read |
| Hazard exposure | one 20-min stream carries every hazard | no single stream >~12k output tokens; timeout class effectively eliminated |
| Coherence risk | intra-call drift over 40k+ tokens (live: run 141's self-introduced §102 incoherence) | numbers/rosters coherent **by construction**; residual cross-slice prose-tone risk (below) |

The honest headline: happy-path wall-clock saving is modest (~40%); the real wins are **failure cost** (the dominant live pain — the attempt is no longer the unit of loss), **tail latency** (p95 today is retry-dominated), and moving the numbers gate ahead of the spend.

### 2.E Riskiest failure mode + mitigation

**Skeleton–slice divergence.** A slice "improves" a decision — nudges a target, drops a themed action, or writes medium-horizon prose contradicting the short horizon's posture. Layered mitigation, mostly deterministic: (1) everything structural/numeric is **locked and byte-restored** at assembly — divergence there is physically impossible, exactly the patch-mode honesty guarantee; (2) prose divergence is bounded because every slice sees the full skeleton (all postures + the whole targets table) — it never has to guess a sibling's numbers; (3) the deterministic headline scrub, numeric/coherence gates, plan-risk-kernel invariants, and the blind whole-artifact reader run over the assembled document unchanged — cross-slice prose contradiction is precisely what the reader judges; (4) residual risk — narrative-tone inconsistency phrased without literal figures that the reader misses — is the same residual the monolith has today, where a 40k-token generation demonstrably drifts against itself (run 141). Second risk — **a wrong skeleton propagates everywhere**: true, but that is today's behavior too (the monolith decides once); the skeleton gate makes the wrong-numbers subclass *cheaper* to catch, not more likely.

### 2.F The simpler alternative — monolith + persist-partial + resume-from-truncation — and why it loses

Evaluated seriously, rejected. (a) The observed failures are **not clean truncations**: run 149's attempts 2/3 were syntax errors at chars 9,036 / 6,756 mid-stream; chars 44k/50k on earlier runs likewise — a corrupt prefix, not a resumable one. (b) The structured-output path (`--json-schema`) validates a *whole document*; there is no supported "continue exactly from this byte" mode, and prompt-level prefix continuation is drift-prone (re-opened objects, restated keys) — we would be building a bespoke JSON splicer to trust model-continued text, with no schema safety net at the seam. (c) It does nothing for the timeout class: the 900s ceiling is per-call, and a resume call carrying the full corpus + the prefix is *bigger* than the original. (d) It does nothing for coherence, the early numbers gate, or schema-validation failures (which require full re-emission anyway). (e) Where it wins — zero new schema, zero coherence risk — matters less than it appears, because the two-stage design also introduces no new *artifact* schema (slices emit existing types; only the compact `PlanSkeleton` is new) and its coherence risk is confined to prose. Resume-from-truncation optimizes the corpse; slicing prevents the death.

## 3. What this deliberately does NOT do

- **No weakening of the blind gates.** Phases 4/4.5/5, the reader, and every deterministic gate stay full-artifact over the assembled output and are not told it was sliced.
- **No new artifact schema.** Slices emit existing `HorizonSection`/`Section` types; `PlanSkeleton` is an internal phase-3 intermediate, persisted only as a sub-checkpoint, never a `plan_versions` surface.
- **No replacement of patch mode.** Concrete-corrections passes still take `_run_phase_3_patch` (strictly cheaper). This design covers the FULL verdict and from-scratch runs.
- **No skeleton judgment authority.** The skeleton gate verifies values against the deterministic manifest; it never decides what is *right* — same doctrine as `blast_radius.py` / `patch_reachability.py`.
- **No convergence loop.** One skeleton retry on gate failure; two transient retries per slice; then fail loud with sub-checkpoints intact for resume.

## 4. Touchpoints

| Change | File |
|---|---|
| NEW `PlanSkeleton` types + skeleton agent | `argosy/agents/plan_skeleton_synthesizer.py` |
| NEW slice-expansion agent (existing output schemas) | `argosy/agents/plan_slice_synthesizer.py` |
| NEW deterministic skeleton gate (manifest floor, roster/coverage/delta checks) | `argosy/quality/skeleton_gate.py` |
| NEW `_run_phase_3_sliced`: skeleton call + gate + fan-out + per-slice retry + sub-checkpoints + assembly with lock enforcement; tier select + `ARGOSY_SLICED_SYNTH` + fail-soft | new module `argosy/orchestrator/flows/plan_synthesis/sliced_phase3.py` |
| Sub-checkpoint writer/loader (`synthesis.phase_3.skeleton` / `.slice.<name>`, skeleton-hash keyed); resume re-runs dead slices only | `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` |
| Reuse: manifest (`resolve_plan_numbers`), value scan (`corrections_check`), force-preserve merge pattern (`_merge_patched_output`), per-agent retry envelope | existing — no changes required |

## 5. Test plan

- **Skeleton gate units:** manifest mismatch fails; `[derivation pending]` passes; corrective wrong-value present fails; `no_change`+non-empty deltas fails; coverage floor; ONE retry then abort.
- **Assembly units:** adversarial stub slice that mutates every locked field → assembled output byte-matches skeleton locks; roster entry omitted by a slice → loud failure; invented sections/deltas dropped; pydantic round-trip.
- **Fan-out/salvage:** one slice raising transiently → siblings complete, slice retried, sub-checkpoint rows written per completed slice; slice dead after retries → phase fails, resume re-runs only that slice; skeleton-hash mismatch invalidates all slice checkpoints.
- **Orchestrator:** flag OFF = today byte-identical; patch-verdict precedence over sliced; skeleton/assembly exception degrades to monolith (logged).
- **Gates downstream:** assembled artifact passes rewriter invariants, speculation cap, plan-risk-kernel, numeric gates — identical fixtures to the monolith path.
- **Live acceptance:** re-run the run-149 class (corrective FULL, 9 findings + 1 directive) under `ARGOSY_SLICED_SYNTH=1`; verify-run it; confirm skeleton gate passed pre-fan-out, no single call exceeded ~12k output tokens, phase-3 wall-clock < 15 min, and any transient retry cost one slice, not the artifact.
