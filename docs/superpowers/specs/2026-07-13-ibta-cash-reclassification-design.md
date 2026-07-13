# IBTA → Cash & T-bills reclassification — Design Spec

**Status:** design approved (Approach 1); ready for implementation plan  
**Date:** 2026-07-13  
**Owner-directed, reversible, NO synthesis.** Staged draft + `/accept` only. Do **not** hand-edit the plan-doc row.

---

## 0. Goal

Reclassify IBTA from **Short-duration IG bonds** → **Cash & T-bills (incl. ILS tranche)** across the whole plan:

1. Engine defaults change so this plan **and** future synthesis agree.
2. Live CURRENT plan is updated via staged draft → `/accept` (no LLM, no full re-synthesis).
3. After accept: allocation-breakdown shows IBTA under Cash & T-bills, **no** Short-duration row (not a 0% leftover), class weights sum to 100%; `/plan` and `/retirement` agree.

---

## 1. Graph-addressability verdict

Instrument-class membership is **not** graph-addressable.

The derivation graph (`allocation_graph.build_allocation_nodes`) exposes only:

- `allocation.sleeve_target.<label>` (INPUT)
- `allocation.normalized` (DERIVED)
- `allocation.single_name_cap` (INPUT)

IBTA membership lives in engine constants (`allocation_plan._FI_BONDS` / `_FI_CASH`) and the plan doc’s `classes[].instruments[]`. Finding routing already treats membership as prose-routed (no single figure node).

**Therefore:** do **not** extend `post_plan_refine` with an instrument-reclassification change-node. Use a **doc-level scoped edit** driven by an engine default change + existing refine draft → accept path.

---

## 2. Architecture (Approach 1)

```
engine change (_FI_CASH includes IBTA; stop emitting Short-duration; all FI → Cash)
  → EARLY PROOF: create_refinement_draft re-derives doc from engine
       assert IBTA ∈ Cash instruments
       assert no Short-duration class row
       assert Cash target ≈ prior Cash+Short (9.93) with Short override cleared, Cash unpinned
  → fold migration: durable "Short-duration IG bonds" override → Cash, key dropped
  → stage draft via POST /plan/refine (clear Short key; leave Cash unpinned if engine yields 9.93)
  → POST /draft/{id}/accept (gates unchanged)
  → live surfaces read re-derived target_allocation_json
```

### 2.1 Engine (`argosy/services/allocation_plan.py`)

| Piece | Change |
|---|---|
| `_FI_CASH.instruments` | IB01 `role=primary`, `weight_within_class_pct=100`; IBTA `role=alt`, `weight_within_class_pct=0` (schema allows `primary`/`alt`/`hold`/`exit` — not “secondary”) |
| `_FI_BONDS` emission | Stop adding Short-duration to `weights` and `classes` |
| FI split | All derived FI → Cash (`CASH_FRAC_OF_FI = 1.0` or delete the split); `bonds_pct` always 0 / callers updated |
| Cash rationale | Note IBTA as held alt (membership; deploy still prefers IB01) |

### 2.2 Override fold migration

Extend the existing label-migration path (`normalize_override_labels` / sibling):

- Durable key `"Short-duration IG bonds"` → **add its pct into Cash**, then **drop the key**.
- Prevents production 400 on unknown label after the sleeve is retired.
- Does **not** invent a new Cash pin when Short was absent.

### 2.3 Live plan apply

`create_refinement_draft` already:

1. Merges overrides
2. Validates via `build_target_allocation`
3. Builds **fresh** `target_allocation_json` via `build_target_allocation_doc` → `build_target_allocation`

Instruments come from **engine constants**, not the frozen CURRENT doc (only the high-growth sleeve is carried via `_fixed_sleeves_from_current`). That re-derivation is the load-bearing path for membership.

**Cash override policy (no double-count):**

- After engine change + clearing/folding Short, derived Cash should already equal prior Cash+Short (6.95+2.98=**9.93**) given remaining overrides.
- Prefer: **clear Short key only; leave Cash unpinned** if the engine already yields 9.93.
- Do **not** stack an explicit Cash→9.93 on top of an engine that already folded Short into Cash (risk of 9.93+2.98 or validation mismatch).
- An explicit Cash→9.93 pin is allowed only as an **idempotent match** to the engine result, never as an additive edit.

### 2.4 Accept gate

`POST /api/plan/draft/{id}/accept` — **unchanged**. Preserve coherence / cap / promote / leakage / FM / corrective gates. No hand-edit of `plan_versions` rows.

### 2.5 Instrument→class map (DB) — explicit choice

`allocation_breakdown` resolves sleeves **plan-first** (`plan_symbol_labels` from the live doc, then DB map). After accept with a re-derived doc, IBTA shows under Cash **even if** a stale `instrument_plan_classes` row still says Short-duration.

`seed_from_plan` / `seed_all` are invoked from the portfolio classification path (`portfolio.py`), **not** from `/accept`. When seed next runs:

- source=`plan` upsert overwrites a prior plan-source IBTA→Short row to IBTA→Cash
- owner rows are never clobbered by plan seed; plan-first display still wins for breakdown

**Choice for this work:** do **not** add accept-time re-seed. Rely on plan-first for correctness after accept; optional follow-up can call `seed_from_plan` on the new current doc if we want the DB map cleaned immediately. Document this so a stale DB row is not treated as a bug.

Update the comment in `instrument_plan_class.py` that says IBTA is deliberately left off the fleet seed because plan-first keeps it on Short-duration (v91) — after this change plan-first keeps it on Cash.

---

## 3. Hard acceptance assertions (pass/fail gates)

### Gate A — Re-derive (prove early, not at the end)

After the engine change, a test (or scripted proof) that calls `create_refinement_draft` with Short-duration cleared from overrides (and **without** patching a frozen v91 doc) must show the draft’s `target_allocation_json`:

1. IBTA listed under **Cash & T-bills (incl. ILS tranche)**
2. **No** class row labeled **Short-duration IG bonds** (absence, not `target_pct=0`)

If this fails, Approach 1 is wrong and we need an explicit engine-rebuild / doc-mutation path — stop and escalate.

### Gate B — No stacked Cash / no double-count

Same early proof:

1. With remaining non-FI overrides held constant and Short key cleared/folded, engine-derived Cash target equals prior Cash+Short (**≈9.93**).
2. Refine does **not** add Short’s weight on top of an already-collapsed Cash figure.

### Gate C — Live after accept

- `/portfolio/allocation-breakdown`: IBTA under Cash; no Short-duration row; class targets sum to 100%
- `/plan` and `/retirement` agree with the same doc
- Accept-gate coherence/cap checks still enforced

---

## 4. Components / files

| File | Role |
|---|---|
| `argosy/services/allocation_plan.py` | Engine defaults, FI split, stop emitting Short-duration |
| `argosy/services/allocation_plan.py` (`normalize_override_labels` or sibling) | Fold Short → Cash in durable overrides |
| `argosy/services/plan_refinement.py` | Unchanged shape; consumes fold + engine rebuild |
| `argosy/api/routes/plan.py` `post_plan_refine` | Apply path still sleeve-target-only; Short key handled by fold |
| `argosy/services/instrument_plan_class.py` | Comment / any seed note for IBTA |
| Tests under `tests/` | Gates A/B, fold fixture, refine/coherence; consumer updates |

No new DB migration unless a forgotten consumer requires schema — not expected.

---

## 5. Testing

1. **Early Gate A+B proof** — `create_refinement_draft` re-derive assertions (membership + Cash≈9.93, no Short row).
2. **Fixture-fold assertion** — durable overrides JSON that still carries `"Short-duration IG bonds": <pct>` → after normalize/fold, that pct is in Cash, Short key is gone (the path most likely to 400 in production).
3. **Refine / scoped-edit** — apply path stages a draft; dry_run still previews; unknown non-migrated labels still 400.
4. **Coherence / accept gates** — existing accept-gate tests remain green; no bypass.
5. **Full-repo grep (hard finish step)** — before calling the work done, grep the whole repo for:
   - `_FI_BONDS`
   - `bonds_pct`
   - `CASH_FRAC_OF_FI`
   - `"Short-duration IG bonds"`
   
   Update every remaining consumer (deploy gaps, FI methodology, retirement sizing, IPS gates, glide fixtures, tests). Same class of miss as F4 catch-all. Zero silent breakages.

6. **Codex-tandem** on fold + no-double-count edit semantics (Layout A; stage a tight `tmp_review/` dir; `sandbox="danger-full-access"` on Windows).

---

## 6. Out of scope

- New derivation-graph instrument membership nodes
- General `__instrument_membership__` override channel
- Full plan re-synthesis / LLM authorship
- Hand-editing `plan_versions.target_allocation_json`
- Accept-time `seed_from_plan` (deferred; plan-first is sufficient)

---

## 7. Reversibility

Revert the engine defaults (restore `_FI_BONDS` + 70/30 split) and stage a new refine draft that re-derives — same draft→accept path. No irreversible schema change.

---

## 8. Decisions locked

| Decision | Choice |
|---|---|
| Graph-addressable? | No → doc-level scoped edit |
| Durability | Engine default change (Approach A) |
| Future FI sizing | All derived FI → Cash |
| Within-Cash weights | IB01 primary 100%; IBTA alt 0% |
| Implementation approach | Approach 1 (engine + existing refine draft) |
| Cash pin on refine | Prefer clear Short only; leave Cash unpinned if engine yields 9.93 |
| Stale DB map row | Leave until next `seed_all`; plan-first wins for live breakdown |
