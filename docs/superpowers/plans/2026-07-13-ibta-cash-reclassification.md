# IBTA → Cash & T-bills Reclassification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reclassify IBTA from Short-duration IG bonds into Cash & T-bills across engine defaults and the live plan via staged draft→accept (no synthesis, no hand-edit).

**Architecture:** Membership is not graph-addressable. Change engine so all FI lands in Cash (IB01 primary 100%, IBTA alt 0%); stop emitting Short-duration; fold durable `"Short-duration IG bonds"` overrides into Cash. Stage live plan via existing `create_refinement_draft` (re-derives doc from engine). Prove Gates A/B early before consumer sweep.

**Tech Stack:** Python 3.12, SQLAlchemy, FastAPI plan refine/accept routes, pytest, codex-tandem (Layout A) for fold/no-double-count review.

**Spec:** `docs/superpowers/specs/2026-07-13-ibta-cash-reclassification-design.md`

## Global Constraints

- Owner-directed, reversible, **NO synthesis / NO LLM**.
- Staged draft + `POST /api/plan/draft/{id}/accept` only — **do not** hand-edit `plan_versions` rows.
- Prefer **clear Short key only; leave Cash unpinned** if engine already yields ≈9.93 (no stacked Cash override).
- Accept-gate coherence/cap/promote/leakage/FM/corrective checks **unchanged**.
- Role for IBTA must be `"alt"` (schema: `primary|alt|hold|exit`).
- Full-repo grep for `_FI_BONDS`, `bonds_pct`, `CASH_FRAC_OF_FI`, `"Short-duration IG bonds"` is a **hard finish step**.
- Stale `instrument_plan_classes` IBTA→Short row: leave until next `seed_all`; plan-first wins for breakdown.
- Codex-tandem on fold + no-double-count semantics before claiming done.

## File map

| File | Responsibility |
|---|---|
| `argosy/services/allocation_plan.py` | Engine: Cash instruments, FI→Cash only, fold migration, `bonds_pct=0`, retire Short emission |
| `argosy/services/target_allocation_doc.py` | Defensive snapshot split: bonds share → Cash label |
| `argosy/services/instrument_plan_class.py` | Update IBTA comment |
| `argosy/services/plan_refinement.py` | No API change; consumes fold via `normalize_override_labels` |
| `tests/test_ibta_cash_reclassification.py` | Gates A/B + fixture-fold (new) |
| Existing tests listed in Task 4 | Consumer / fixture updates |

---

### Task 1: Fold migration + failing tests (TDD)

**Files:**
- Modify: `argosy/services/allocation_plan.py` (`normalize_override_labels`, constants near `SLEEVE_LABEL_ALIASES`)
- Create: `tests/test_ibta_cash_reclassification.py`

**Interfaces:**
- Produces: `SHORT_DURATION_IG_LABEL: str`, `CASH_LABEL` (or reuse existing string), `RETIRED_SLEEVE_FOLD_INTO: dict[str, str]`, updated `normalize_override_labels(overrides: dict[str, float]) -> dict[str, float]` that (1) applies `SLEEVE_LABEL_ALIASES`, (2) folds retired keys into target by **adding** pct, then drops retired key

- [ ] **Step 1: Write the failing fold + Gate B unit tests**

```python
# tests/test_ibta_cash_reclassification.py
from __future__ import annotations

import json

import pytest

from argosy.services.allocation_plan import (
    normalize_override_labels,
    build_target_allocation,
)


CASH = "Cash & T-bills (incl. ILS tranche)"
SHORT = "Short-duration IG bonds"


def test_fold_short_duration_into_cash_adds_pct_and_drops_key() -> None:
    """Production path: durable overrides still carry Short-duration → must not 400."""
    out = normalize_override_labels({
        CASH: 6.95,
        SHORT: 2.98,
        "Strategic single-stock (NVDA)": 8.0,
    })
    assert SHORT not in out
    assert out[CASH] == pytest.approx(9.93)
    assert out["Strategic single-stock (NVDA)"] == 8.0


def test_fold_short_only_creates_cash_pin() -> None:
    out = normalize_override_labels({SHORT: 2.98})
    assert SHORT not in out
    assert out[CASH] == pytest.approx(2.98)


def test_fold_absent_short_is_noop_for_cash() -> None:
    out = normalize_override_labels({CASH: 6.95})
    assert out == {CASH: 6.95}
```

- [ ] **Step 2: Run tests — expect FAIL**

```powershell
D:\Projects\financial-advisor\.venv\Scripts\python.exe -m pytest tests/test_ibta_cash_reclassification.py -v
```

Expected: FAIL (`SHORT` still present / Cash not summed) or import errors until implementation.

- [ ] **Step 3: Implement fold in `normalize_override_labels`**

In `argosy/services/allocation_plan.py`, next to `SLEEVE_LABEL_ALIASES`:

```python
CASH_LABEL = "Cash & T-bills (incl. ILS tranche)"
SHORT_DURATION_IG_LABEL = "Short-duration IG bonds"  # retired; fold-only
RETIRED_SLEEVE_FOLD_INTO: dict[str, str] = {
    SHORT_DURATION_IG_LABEL: CASH_LABEL,
}
```

Update `normalize_override_labels`:

```python
def normalize_override_labels(overrides: dict[str, float]) -> dict[str, float]:
    if not overrides:
        return dict(overrides or {})
    # 1) legacy alias rename (existing behaviour)
    legacy = {k: v for k, v in overrides.items() if k in SLEEVE_LABEL_ALIASES}
    current = {k: v for k, v in overrides.items() if k not in SLEEVE_LABEL_ALIASES}
    out: dict[str, float] = {}
    for k, v in {**legacy, **current}.items():
        out[SLEEVE_LABEL_ALIASES.get(k, k)] = v
    # 2) fold retired sleeves into their target (add pct, drop key)
    for retired, target in RETIRED_SLEEVE_FOLD_INTO.items():
        if retired not in out:
            continue
        retired_pct = float(out.pop(retired))
        out[target] = float(out.get(target, 0.0)) + retired_pct
    return out
```

Export new names in `__all__` if present.

- [ ] **Step 4: Re-run fold tests — expect PASS**

```powershell
D:\Projects\financial-advisor\.venv\Scripts\python.exe -m pytest tests/test_ibta_cash_reclassification.py -v
```

- [ ] **Step 5: Commit**

```powershell
git add argosy/services/allocation_plan.py tests/test_ibta_cash_reclassification.py
git commit -m "feat(allocation): fold retired Short-duration overrides into Cash"
```

---

### Task 2: Engine defaults — all FI → Cash; IBTA as alt (Gate A early proof)

**Files:**
- Modify: `argosy/services/allocation_plan.py` (`CASH_FRAC_OF_FI`, `_renormalise`, `_FI_CASH`, `_FI_BONDS` emission in `build_target_allocation`, `TargetAllocation.bonds_pct`)
- Modify: `tests/test_ibta_cash_reclassification.py` (add Gate A/B engine + draft proofs)

**Interfaces:**
- Consumes: fold from Task 1
- Produces: `build_target_allocation()` with no Short-duration class; Cash instruments include IB01+IBTA; `bonds_pct == 0`; `cash_pct == fi_pct` (2dp)

- [ ] **Step 1: Write failing Gate A/B engine tests**

Append to `tests/test_ibta_cash_reclassification.py`:

```python
def test_engine_emits_ibta_under_cash_and_no_short_row() -> None:
    alloc = build_target_allocation()
    labels = [c.label for c in alloc.classes]
    assert SHORT not in labels
    cash = next(c for c in alloc.classes if c.label == CASH)
    syms = {i.symbol: i for i in cash.instruments}
    assert "IB01" in syms and syms["IB01"].role == "primary"
    assert syms["IB01"].weight_within_class_pct == pytest.approx(100.0)
    assert "IBTA" in syms and syms["IBTA"].role == "alt"
    assert syms["IBTA"].weight_within_class_pct == pytest.approx(0.0)
    assert alloc.bonds_pct == pytest.approx(0.0)
    assert alloc.cash_pct == pytest.approx(alloc.fi_pct, abs=0.02)


def test_engine_cash_equals_prior_cash_plus_short_under_pinned_fi_split() -> None:
    """Gate B: with Cash+Short pinned as on v91-style overrides, fold → Cash≈9.93."""
    pinned = normalize_override_labels({CASH: 6.95, SHORT: 2.98})
    assert pinned[CASH] == pytest.approx(9.93)
    alloc = build_target_allocation(authored_overrides=pinned)
    cash = next(c for c in alloc.classes if c.label == CASH)
    assert cash.target_pct == pytest.approx(9.93, abs=0.02)
    assert SHORT not in {c.label for c in alloc.classes}
```

- [ ] **Step 2: Run — expect FAIL** (IBTA still under Short / Short row still emitted)

```powershell
D:\Projects\financial-advisor\.venv\Scripts\python.exe -m pytest tests/test_ibta_cash_reclassification.py::test_engine_emits_ibta_under_cash_and_no_short_row -v
```

- [ ] **Step 3: Implement engine change**

1. Set `CASH_FRAC_OF_FI = 1.0` and update module docstring (or delete split and assign `weights[CASH_LABEL] = fi_pct` only — prefer delete Short weight line entirely).
2. `_renormalise`: only set Cash weight to `fi_pct`; **do not** set Short-duration key.
3. `_FI_CASH.instruments`:

```python
instruments=(
    AllocationInstrument(
        symbol="IB01", role="primary", weight_within_class_pct=100.0, domicile="IE",
        rationale=(...existing IB01 text...),
    ),
    AllocationInstrument(
        symbol="IBTA", role="alt", weight_within_class_pct=0.0, domicile="IE",
        rationale=(
            "1-3yr US Treasuries via Irish UCITS IBTA — held alt inside Cash & T-bills "
            "(owner reclass from retired Short-duration IG bonds). Deploy prefers IB01; "
            "IBTA remains estate-safe UCITS membership in this sleeve."
        ),
    ),
),
```

4. In `build_target_allocation`: append only Cash class; **do not** append `_FI_BONDS`. Set `bonds_pct = 0.0`. `reported_fi_pct = cash_pct`.
5. Keep `_FI_BONDS` constant temporarily **only if** tests still import it — Task 4 removes or updates those imports. Prefer deleting emission first; leave constant until Task 4 grep cleanup deletes it or marks retired.

- [ ] **Step 4: Run engine Gate tests — expect PASS**

```powershell
D:\Projects\financial-advisor\.venv\Scripts\python.exe -m pytest tests/test_ibta_cash_reclassification.py -v
```

- [ ] **Step 5: Write failing `create_refinement_draft` Gate A proof**

```python
def test_create_refinement_draft_rederives_ibta_under_cash(client_with_db) -> None:
    """Gate A: draft doc re-derived from engine — IBTA under Cash, no Short row."""
    from argosy.state.models import PlanVersion
    from argosy.services.plan_refinement import create_refinement_draft
    import unittest.mock as mock

    SF = client_with_db.app.state.session_factory
    with SF() as session:
        session.add(PlanVersion(
            user_id="ariel",
            role="current",
            version_label="gate-a-base",
            source_path="", raw_markdown="",
            target_allocation_overrides_json=json.dumps({
                CASH: 6.95,
                SHORT: 2.98,
            }),
        ))
        session.commit()

    # Provide a trivial conserving composition so doc build succeeds.
    fake_comp = {
        CASH: 10.0,
        "US broad-market core": 40.0,
        "Strategic single-stock (NVDA)": 8.0,
        "Dividend-quality income": 12.0,
        "Global quality growth (ex-NVDA-dense)": 8.0,
        "International developed (ex-US)": 10.0,
        "Emerging markets": 4.0,
        "US low-volatility equity": 4.0,
        "Real assets (REIT/TIPS)": 4.0,
    }
    # pad to ~100 if needed — use build_target_allocation labels dynamically if flaky

    with mock.patch(
        "argosy.services.target_allocation_doc.load_full_book_today_composition",
        return_value=fake_comp,
    ), mock.patch(
        "argosy.services.target_allocation_doc._prior_glide_q0",
        return_value=None,
    ):
        with SF() as session:
            # Empty new overrides: fold clears Short from existing; leave Cash unpinned
            # only if we pass overrides that omit Cash — here existing fold yields Cash=9.93.
            # Prefer explicit {} so merge+fold runs on existing JSON.
            draft = create_refinement_draft(session, "ariel", {})

    assert draft.target_allocation_json
    doc = json.loads(draft.target_allocation_json)
    labels = [c["label"] for c in doc["classes"]]
    assert SHORT not in labels
    cash = next(c for c in doc["classes"] if c["label"] == CASH)
    assert any(i["symbol"] == "IBTA" for i in cash["instruments"])
    assert cash["target_pct"] == pytest.approx(9.93, abs=0.05)
    merged = json.loads(draft.target_allocation_overrides_json)
    assert SHORT not in merged
    assert merged.get(CASH) == pytest.approx(9.93)
```

If empty `{}` cannot stage a draft (service requires a change), pass a no-op sleeve override that already matches engine for a non-FI label, or call `create_refinement_draft` after manually ensuring fold runs — **do not** stack Cash+=2.98 on top of folded Cash.

- [ ] **Step 6: Run Gate A draft test — expect PASS** (if FAIL because doc still has Short → **STOP and escalate**; Approach 1 is wrong)

```powershell
D:\Projects\financial-advisor\.venv\Scripts\python.exe -m pytest tests/test_ibta_cash_reclassification.py::test_create_refinement_draft_rederives_ibta_under_cash -v
```

- [ ] **Step 7: Commit**

```powershell
git add argosy/services/allocation_plan.py tests/test_ibta_cash_reclassification.py
git commit -m "feat(allocation): move IBTA into Cash; retire Short-duration sleeve"
```

---

### Task 3: Glide / today-composition defensive split → Cash

**Files:**
- Modify: `argosy/services/target_allocation_doc.py` (`derive_full_book_today_composition`, `load_full_book_today_composition`)
- Modify: `tests/test_target_allocation_doc.py`, `tests/test_cross_surface_consistency.py` as needed

**Interfaces:**
- Change `bonds_target` parameter to feed **Cash** label (rename to `cash_target` or keep name but write to `CASH` key). When `bonds_target`/`cash_target` is 0 and low_vol>0, existing denom logic still works; when Cash has the FI weight, defensive share that used to go to Short goes to Cash.

- [ ] **Step 1: Update failing composition tests** that assert `comp["Short-duration IG bonds"]`

In `tests/test_target_allocation_doc.py`: replace Short-duration expectations with Cash (recompute expected numbers from the same formula with Cash target).

- [ ] **Step 2: Run — expect FAIL**

```powershell
D:\Projects\financial-advisor\.venv\Scripts\python.exe -m pytest tests/test_target_allocation_doc.py -v --tb=line -q
```

- [ ] **Step 3: Implement**

In `derive_full_book_today_composition`, change the defensive branch to write Cash:

```python
comp[CASH_LABEL] = (
    comp.get(CASH_LABEL, 0.0) + scaled * bonds_target / denom)
```

(Use `CASH_LABEL` from `allocation_plan` or the literal string.) Update docstring: defensive splits low-vol + Cash (FI home), not Short-duration.

In `load_full_book_today_composition`:

```python
bonds_target=by_label.get("Cash & T-bills (incl. ILS tranche)", 0.0),
# or rename kwarg to cash_target=...
```

Prefer renaming kwarg to `cash_target` and updating all call sites in this task.

- [ ] **Step 4: Re-run composition tests — PASS**

- [ ] **Step 5: Commit**

```powershell
git commit -am "fix(alloc-doc): map defensive snapshot FI share to Cash sleeve"
```

---

### Task 4: Full-repo consumer grep + fixture updates (hard finish step)

**Files:** every hit from the grep below (tests + services; **do not** rewrite SDD historical narrative unless a mechanism sentence is now false — update mechanism lines only).

- [ ] **Step 1: Grep (hard)**

```powershell
rg -n "_FI_BONDS|bonds_pct|CASH_FRAC_OF_FI|Short-duration IG bonds" --glob "!docs/superpowers/**" --glob "!.git/**"
```

Update every remaining consumer:

| Likely hit | Action |
|---|---|
| `tests/test_allocation_plan.py` | Cash = full FI; drop Short from comps; `CASH_FRAC_OF_FI` may remain `1.0` or be removed |
| `tests/test_allocation_plan_overrides.py` | `bonds_pct == 0` still OK |
| `tests/services/deployment_funnel/test_look_through.py` | Drop `ap._FI_BONDS` from sleeve list |
| `tests/test_ips_equality_gate.py` | Fixture prose/targets: fold Short into Cash |
| `tests/test_allocation_glidepath.py` | Remove Short waypoint or map to Cash |
| `tests/test_plan_output_gate.py` | Drop Short `_pct` row / fold into Cash |
| `tests/test_instrument_plan_class.py` | Keep plan-first test but use Cash as plan label for IBTA (or keep Short as “whatever plan says” with comment) |
| `tests/test_allocation_breakdown.py` | Already asserts no Short — keep |
| `argosy/services/instrument_plan_class.py` | Update IBTA comment: plan-first keeps it on Cash |
| `argosy/services/allocation_plan.py` | Remove dead `_FI_BONDS` if unused; export cleanup |

- [ ] **Step 2: Run focused suites**

```powershell
D:\Projects\financial-advisor\.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_ibta_cash_reclassification.py tests/test_allocation_plan.py tests/test_allocation_plan_overrides.py tests/test_target_allocation_doc.py tests/test_plan_refine_route.py tests/test_instrument_plan_class.py tests/test_allocation_breakdown.py tests/test_allocation_glidepath.py tests/test_ips_equality_gate.py tests/test_cross_surface_consistency.py tests/test_plan_output_gate.py tests/services/deployment_funnel/test_look_through.py -q
```

- [ ] **Step 3: Re-grep until only intentional leftovers remain** (retired label constant, fold map, this plan/spec, historical handover docs if any)

- [ ] **Step 4: Commit**

```powershell
git commit -am "fix: retire Short-duration IG bonds consumers after Cash fold"
```

---

### Task 5: Codex-tandem on fold + no-double-count semantics

**Files:**
- Stage: `tmp_review/ibta-cash-reclass/` (copy only `allocation_plan.py` normalize+engine sections, `test_ibta_cash_reclassification.py`, design spec excerpt)

- [ ] **Step 1: Stage tight review dir** (never `node_dir` = repo root)

- [ ] **Step 2: Dispatch codex reviewer** (Layout A; Windows needs `sandbox="danger-full-access"`). Prompt must ask to re-derive:

  1. Fold adds Short pct into Cash and drops key (no double-count when both present).
  2. Engine all-FI→Cash + IBTA alt/0 does not leave a Short row.
  3. Refine path must not stack Cash→9.93 on top of folded 9.93.

- [ ] **Step 3: Address BLOCKERS; re-run Gate A/B tests**

- [ ] **Step 4: Commit any fixes**

```powershell
git commit -am "fix(allocation): address tandem review on IBTA Cash fold"
```

---

### Task 6: Live apply (owner machine) — stage draft + accept

**Not a code change** — operational proof on Ariel’s CURRENT plan.

- [ ] **Step 1: Dry-run refine** (any sleeve-target preview OK; confirm fold won’t 400)

- [ ] **Step 2: Apply refine** that triggers re-derivation with **no stacked Cash pin**:
  - If CURRENT overrides still contain Short: apply `{}` or a no-op-compatible change such that `create_refinement_draft` merges+folds existing overrides (Cash becomes 9.93 via fold, not via additive pin).
  - If the route requires a non-empty `changes` list, supply a sleeve change that is already at the engine value for a non-FI sleeve, **or** extend apply path minimally to accept empty changes when only fold migration is needed — prefer **not** inventing API; use fold-on-merge by passing one existing override unchanged if required.

- [ ] **Step 3: Accept draft** via `POST /api/plan/draft/{id}/accept` (no overrides of gates unless already needed for unrelated reasons).

- [ ] **Step 4: Gate C live checks**

  - `/portfolio/allocation-breakdown`: IBTA under Cash; no Short-duration row; targets sum ≈100%
  - `/plan` + `/retirement` agree
  - Optional: hit portfolio classification seed path so DB map row updates; not required for breakdown correctness

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|---|---|
| Not graph-addressable → no refine change-node | Global + Task 2/6 use existing refine |
| Engine IB01 primary / IBTA alt 0% | Task 2 |
| All FI → Cash; drop Short emission | Task 2 |
| Fold Short→Cash in overrides | Task 1 |
| Gate A early re-derive proof | Task 2 Step 5–6 |
| Gate B no stacked Cash | Task 1–2 + Task 5 + Task 6 |
| Accept gates unchanged | Task 6 |
| Fixture-fold assertion | Task 1 |
| Full-repo grep finish | Task 4 |
| Stale DB map / plan-first | Spec §2.5; Task 4 comment; Task 6 optional seed |
| Codex-tandem fold/no-double-count | Task 5 |
| Live Gate C | Task 6 |

No TBD placeholders. Types: `normalize_override_labels(dict[str,float]) -> dict[str,float]` consistent across tasks.
