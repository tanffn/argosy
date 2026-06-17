# Implementation plan — derivation-first plan architecture

Spec: `docs/superpowers/specs/2026-06-18-derivation-first-plan-design.md`. TDD, fail-closed.
Goal: make "derive, never inherit; one canonical object; re-derive don't ratify; one
promote gate" structural, so the 2-day tweak loop cannot recur.

## Slice 1 — typed `PlanDecisionModel` (the canonical object)
- New `argosy/quality/plan_model.py`: `Input` (value + provenance: `source`,
  `as_of`, `kind='input'`) and `Derived` (value + `formula` + `inputs_used` +
  `kind='derived'`). `PlanDecisionModel` holds both maps; **construction REJECTS a
  `Derived` whose value was copied from an `Input`/prior-doc target** (provenance check:
  a derived value must name a formula + the inputs it consumed, never `source='plan_doc'`
  / `source='prior_target'`).
- Tests: a derived value with `formula` + `inputs_used` validates; one with
  `source='plan_doc'` raises `InheritedTargetError`; round-trips to/from JSON.

## Slice 2 — derivation functions (pure, from inputs only)
- `argosy/services/plan_derivation.py`: pure fns producing `Derived` values —
  `derive_nvda_deconcentration(book, nvda_px, nvda_sh, cap, target_w, tax_cliff)`,
  `derive_fi_margin_liquid(liquid_nw, fi_total_capital)`,
  `derive_earliest_safe_date(...)`. Each returns value + formula string + inputs_used.
- Port the validated arithmetic from `tmp_review/derive_plan.py` (codex-zigzag-confirmed).
- Tests: lock the known results (NVDA target ≈ 2,202, sell ≈ 9,269; FI liquid margin
  ≈ −₪148K) against fixed inputs; assert FI uses the LIQUID basis, never investable.

## Slice 3 — re-derivation reviewer (re-derive, don't ratify)
- `argosy/quality/rederivation_reviewer.py`: given a `PlanDecisionModel`, RECOMPUTE every
  `Derived` from its `inputs_used` via the Slice-2 fns, blind to the stored value; BLOCK on
  any divergence beyond tolerance. A stored value with no re-derivable formula → BLOCK
  (no orphan numbers).
- Tests: a model whose stored NVDA target was tampered (e.g. 3,000) → BLOCK with the
  recomputed value; a clean model → PASS.

## Slice 4 — single fail-closed promote gate
- Extend the promote path: `can_promote(model, authorities)` returns False if ANY
  authority (codex / deterministic gate / FM / reader / re-derivation reviewer) is BLOCK.
  On promote: relabel the version slug (strip `-fm-rejected`), strip stale gate receipts,
  set `role='current'` ONLY when all clear.
- Tests: promotion refused while any authority BLOCKs; slug relabeled on a clean promote;
  the draft-45 scenario (codex BLOCK + gate FAIL + FM reject) cannot reach `current`.

## Slice 5 — refuse to synthesize on stale/low-confidence inputs
- Pre-synthesis guard: if holdings snapshot age > threshold, or a load-bearing input
  (savings floor) is flagged LOW confidence, BLOCK + emit a `needs_refresh` finding routed
  to ingest — do not produce a plan that is "one trade stale."
- Tests: stale snapshot → guard BLOCKs with `needs_refresh`; fresh → proceeds.

## Slice 6 — render surfaces as pure projections
- MD bodies / dashboard / actions JSON / `/retirement` render FROM the `PlanDecisionModel`
  only. A cross-surface consistency test asserts every surface's NVDA target / FI basis /
  retirement date equals the model's — contradiction impossible by construction.

## Sequencing
1–2 first (model + derivations, the substance), then 3–4 (the governance that stops the
draft-45 class), then 5–6. Each slice: tests first, fail-closed, commit per slice.
Smoke per change; full suite at the end.
