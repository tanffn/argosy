# Handover — 2026-07-03 · plan/execution separation + incremental plan refinement (SHIPPED to master)

**Branch:** `master` · **HEAD:** `4a7b782` · tree clean. Everything below is merged + tested.
Read this first in a fresh session, then `git log --oneline -30`.

---

## 1. START HERE — what this session built (three arcs, all on master)

**Arc A — Plan vs Execution separation + regime data + the anti-correlation gate.**
- **SDD §1.7** now defines the separation: the **plan** is strategic policy (5–30y, regime-*robust*, only *secular* shifts via a governed refresh); **execution** (deploy-cash / the fleet author) is tactical (0–5y, regime-*aware*, maneuvers within the plan's bands + raises a plan-revisit flag, never rewrites the plan). See [[feedback_plan_execution_separation]].
- **Regime feed enriched** (`market_snapshot.py`): added real yields (DFII10), breakevens (T10YIE), IG/HY credit spreads, fed funds, 10y — fed to the deployment author (execution), NOT the plan fleet. (`e2ff9d3`, `de2a8e9`)
- **Blind anti-correlation gate** at plan promotion (`/accept`): the deterministic kernel re-derives the DRAFT's TARGET single-name look-through and blocks a plan that breaches its own cap. **Enforced by default** now (`plan_lookthrough_gate_enforce=True`, fail-closed; conftest guard forces it off in tests). (`649406a`, `3511f77`)

**Arc B — Incremental plan refinement decision core** (edit-don't-rebuild, blast-radius-sized). Merged `5bff213`.
- `argosy/quality/plan_risk_kernel.py` — deterministic money-safety kernel: single-name look-through cap + allocation-sum + us-situs (fail-closed) + `evaluate_plan_invariants`. Also `evaluate_plan_target_single_name_cap(doc)`.
- `argosy/quality/plan_node_meta.py` — `node_meta(key) → NodeMeta` (owner_domain / policy_axis / authoring_mode / rebuild_boundary / plan_identity_axis), keyed off the dotted node key; drift-guarded against `_OWNER_BY_PREFIX`.
- `argosy/quality/blast_radius.py` — `size_blast_radius` (on a **sandbox clone**) + `classify` → **T0 scoped_edit / T1 bounded_rederive / T2 full_rebuild** (topology/owner/policy-axis, not counts). `# agents run = f(blast radius)`.
- `argosy/quality/refinement.py` — `run_refinement(...)` orchestration + **invariant-net override** (an incoherent post-state is forced to FULL_REBUILD).
- `scripts/refine_smoke.py` — live decision-core harness.

**Arc C — Allocation-in-graph + the mutation API (durable authored overrides, "option B").** Merged `e5e5d84` + `4a7b782`.
- `argosy/quality/allocation_graph.py` — sleeve targets as **deterministic INPUT nodes** (+ `allocation.normalized` DERIVED renormalize + `allocation.single_name_cap`), so a *supplied* sleeve tweak is a **scoped edit**, not a rebuild.
- **Engine override:** `build_target_allocation(..., authored_overrides=dict[label→pct])` pins the override exactly + renormalizes the remainder proportionally; `overrides=None` is byte-identical. (`d37215e`, `8a83c11`)
- **Durable persistence:** `PlanVersion.target_allocation_overrides_json` (migration **0076**) threaded through the engine and **carried forward on every draft path** → a refined target **survives re-synthesis**. (`1deb3a4`)
- **Apply API:** `POST /api/plan/refine` — `dry_run=true` previews the tier/blast-radius; `dry_run=false` creates a **staged draft** carrying the override (validate-before-write; **no auto-promote**; unknown-node→400, full-rebuild/non-allocation→409, `base_plan_version` mismatch→409). (`2d6f907`, `6113142`, `c9e6fdf`)
- **Rollback:** `POST /api/plan/rollback` — atomic revert of `current` to a retained `superseded` version. (`c76f3e1`)

## 2. The functional finding you must not lose (decision-grade)
Running the real refine-engine + kernel on Ariel's live plan (before/after, non-mutating):
- The **current plan breaches its own 13% single-name cap: 16.8%** target look-through (direct 12% NVDA + embedded NVDA in the US sleeves). This is why the gate now blocks it.
- **The reviewers' sleeve-only fix (US-core 31.5→24, growth 13.2→4, real-assets 2→6) makes it WORSE (18.0%)** — because pinning the US-equity sleeves down and renormalizing the remainder *proportionally inflates the un-pinned NVDA sleeve* (12%→14.9%). You cannot fix a single-name look-through breach by trimming only the *other* sleeves.
- **The cap only clears by lowering the NVDA *target* itself:** NVDA 12→8 (with core24/growth4/real6) → **11.3% → passes.** NVDA→6 → 8.7%. That is exactly what the glide/sell-schedule does over time, so the plan's cap becomes satisfiable as NVDA winds down.

## 3. The process discipline (keep doing this) — [[feedback_adversarial_review_must_re_derive_blind]]
Every subagent task was **blind-reviewed** (a reviewer given raw code + a refute-mandate, not the manifest) and **verified firsthand** (run it, read the output). This caught real bugs a green suite hid at nearly every step: fail-open us_situs; **two dead T2 triggers** (silent under-escalation in the classifier); a `cls.id` vs real `.label` **live bug** (a whole task was false-green on real data); an `UnknownNodeError`→**500** on valid input; rationale/number staleness. Adjudicate each finding (verify facts, decide judgments, reject false positives) — the reviewer is a claim, not authority; **zigzag, don't rubber-stamp.**

## 4. Verify (fast) / run
- Refinement stack (247 tests): `pytest -m "not llm_eval" tests/test_plan_risk_kernel.py tests/test_plan_node_meta.py tests/test_blast_radius.py tests/test_refinement.py tests/test_allocation_graph.py tests/test_allocation_plan.py tests/test_allocation_plan_overrides.py tests/test_authored_overrides_durability.py tests/test_plan_refine_route.py tests/test_plan_rollback_route.py tests/test_promote_gate.py`
- `/accept` route (gate default-on): `tests/test_plan_draft_api.py` → 53 passed (~6 min).
- Live decision-core harness: `.venv/Scripts/python.exe scripts/refine_smoke.py`.
- Design spec (context): `docs/superpowers/specs/2026-07-03-incremental-plan-refinement.md` (v3, codex-reviewed + blind-code-verified). Build roadmap: `docs/handovers/2026-07-03-plan-execution-separation-build.md`.

## 5. NEXT — open items (none blocking; ranked)
1. **Stage a cap-clearing refinement** for Ariel to review/promote — `POST /api/plan/refine dry_run=false` with e.g. `{NVDA→8, US-core→24, growth→4, real-assets→6}` (Ariel picks the numbers). Proven to clear the cap to 11.3%.
2. **Sigma-anchor invariant** in the risk kernel — an authored override can move blended_sigma off the derived anchor (intentional for option B), but nothing catches it downstream yet. Add it to `plan_invariants`.
3. **Surface a dropped/invalid override** — `resolve_target_allocation_json` currently logs + silently falls back without overrides on a bad stored value; surface it.
4. **Scalar-node refinement mutation** — only *allocation* sleeve edits mutate today; scalar edits (e.g. SWR anchor) preview but 409 on apply. Needs its own override mechanism.
5. **`/refine` UI** + an **allocation topic-owner agent** (so *unsupplied* figure changes can be authored scoped instead of escalating to T2).
6. **Full re-synthesis comparison** (deferred): re-run the fleet and confirm it reproduces the same allocation defects — proving the fix belongs in refinement/execution, not a regime-chasing plan owner (which we deliberately did NOT build).
