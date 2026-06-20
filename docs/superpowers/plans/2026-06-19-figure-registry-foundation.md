# Canonical Figure Registry — Metadata Foundation (Phase 1a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the canonical figure-registry **metadata layer** that wraps every resolved plan figure with an owner, a kind, semantic identity, materiality, evidence, and a fail-closed validation status — the spine that makes "one owner per figure" enforceable.

**Architecture:** A new pure module `argosy/quality/figure_registry.py` defines `FigureKind`, `Materiality`, `OwnerRole` enums, the `FigureRecord` dataclass, `owner_for(key)` (explicit `OWNER_MAP` overrides + prefix rules so dynamic keys like `allocation.*` are owned by rule, not enumeration), `build_figure_registry(resolved)` wrapping a `ResolvedPlanNumbers` manifest into `FigureRecord`s, and `validate_figure(record)` enforcing evidence-per-kind, materiality-gated. No new derivation math, no LLM, no DB — it annotates existing resolver output.

**Tech Stack:** Python 3.12, frozen dataclasses + `enum.Enum`, pytest. Mirrors `argosy/quality/gate_types.py`.

**Scope (sharpened per codex plan review):** This is **Phase 1a = the registry metadata layer ONLY.** The spec's full Phase 1 is three plans:
- **1a (this plan):** ownership + classification + materiality-gated validation status over the figures the resolver ALREADY produces.
- **1b (follow-on):** add the missing canonical figures (`net_worth.total_incl_residence_nis`, `retirement.fi_crossing_year`, `tax.retention_*`, `concentration.*_pool_sh`/`*_slice_sh`) + real source-freshness from a Data-Steward seam.
- **1c (follow-on):** cut the contradiction-prone surfaces (FI-crossing table, dashboard net-worth, retention, tranche) to registry rendering. 1c is what makes the convergence claim *real* for those surfaces; 1a/1b are prerequisites.

**Honest Phase-1a limitation:** there is no cross-model validator yet (that is Phase 3). So in 1a, deterministic `formula_result`/`source_fact` figures resolve on the resolver's deterministic computation (`validated_by="resolver"`), and material judgment figures (`model_projection`/material `assumption`) that carry evidence but no cross-model check are left **`pending`** (awaiting Phase 3), NOT `blocked`. A judgment with NO evidence is `blocked` (fail-closed). Spec: `docs/superpowers/specs/2026-06-19-financial-advisory-team-design.md`.

---

### Task 1: Enums — `FigureKind`, `Materiality`, `OwnerRole`

**Files:**
- Create: `argosy/quality/figure_registry.py`
- Test: `tests/test_figure_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_figure_registry.py
from argosy.quality.figure_registry import FigureKind, Materiality, OwnerRole


def test_enums_have_expected_members():
    assert FigureKind.FORMULA_RESULT.value == "formula_result"
    assert {k.value for k in FigureKind} == {
        "source_fact", "assumption", "formula_result",
        "model_projection", "interpretation", "recommendation",
    }
    assert {m.value for m in Materiality} == {"high", "medium", "low"}
    assert OwnerRole.RETIREMENT_FI.value == "retirement_fi"
    assert OwnerRole.LEAD_PLANNER.value == "lead_planner"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py::test_enums_have_expected_members -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'argosy.quality.figure_registry'`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/quality/figure_registry.py
"""Canonical figure registry — metadata layer (Phase 1a).

Wraps the deterministic resolver output (``ResolvedPlanNumbers``) with the
ownership + classification + evidence + validation metadata that makes
"one accountable owner per figure" enforceable. Pure: no DB, no LLM, no new
derivation math. See docs/superpowers/specs/2026-06-19-financial-advisory-team-design.md.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum


class FigureKind(str, Enum):
    """How a figure is produced — sets the validation it must clear."""

    SOURCE_FACT = "source_fact"
    ASSUMPTION = "assumption"
    FORMULA_RESULT = "formula_result"
    MODEL_PROJECTION = "model_projection"
    INTERPRETATION = "interpretation"
    RECOMMENDATION = "recommendation"


class Materiality(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class OwnerRole(str, Enum):
    """Generalized firm roster — client-agnostic."""

    LEAD_PLANNER = "lead_planner"
    CLIENT_DISCOVERY = "client_discovery"
    DATA_STEWARD = "data_steward"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW = "cash_flow"
    TAX = "tax"
    INVESTMENT = "investment"
    RETIREMENT_FI = "retirement_fi"
    INSURANCE_RISK = "insurance_risk"
    ESTATE = "estate"
    EQUITY_COMP = "equity_comp"
    COMPLIANCE = "compliance"
    COMMITTEE = "committee"
    OPERATIONS = "operations"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py::test_enums_have_expected_members -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/figure_registry.py tests/test_figure_registry.py
git commit -m "feat(registry): FigureKind / Materiality / OwnerRole enums"
```

---

### Task 2: `FigureRecord` dataclass (full spec shape incl. timestamp)

**Files:**
- Modify: `argosy/quality/figure_registry.py`
- Test: `tests/test_figure_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_figure_registry.py
from argosy.quality.figure_registry import FigureRecord


def test_figure_record_carries_full_identity_and_defaults():
    r = FigureRecord(
        id="retirement.fi_target_nis", value=11_836_133.0, unit="nis",
        kind=FigureKind.FORMULA_RESULT, owner=OwnerRole.RETIREMENT_FI,
    )
    assert r.basis is None and r.scenario is None and r.as_of is None
    assert r.materiality is Materiality.MEDIUM
    assert r.consult == () and r.evidence == ()
    assert r.validated_by == "none"
    assert r.status == "pending"
    assert r.version == 0 and r.timestamp is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py::test_figure_record_carries_full_identity_and_defaults -v`
Expected: FAIL with `ImportError: cannot import name 'FigureRecord'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to argosy/quality/figure_registry.py
@dataclass(frozen=True)
class FigureRecord:
    """One owned figure (or non-numeric claim) with full semantic identity,
    provenance, and validation status. See the spec's FigureRecord shape."""

    id: str
    value: float | str | None
    unit: str
    kind: FigureKind
    owner: OwnerRole
    consult: tuple[OwnerRole, ...] = ()
    basis: str | None = None
    scenario: str | None = None
    as_of: str | None = None
    jurisdiction: str | None = None
    policy_version: str | None = None
    precision: str | None = None
    inputs: tuple[str, ...] = ()
    method: str | None = None
    evidence: tuple[str, ...] = ()
    source_freshness: str | None = None
    confidence: str | None = None
    materiality: Materiality = Materiality.MEDIUM
    validated_by: str = "none"   # none | resolver | recompute | cross_model_rederivation
    status: str = "pending"      # pending | resolved | blocked
    version: int = 0
    timestamp: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py::test_figure_record_carries_full_identity_and_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/figure_registry.py tests/test_figure_registry.py
git commit -m "feat(registry): FigureRecord dataclass with semantic identity"
```

---

### Task 3: `OwnerSpec` + `OWNER_MAP` + `owner_for` (explicit + prefix rules)

**Files:**
- Modify: `argosy/quality/figure_registry.py`
- Test: `tests/test_figure_registry.py`

**Context (codex review):** the resolver produces more keys than `_KEY_UNITS` — `_SYNTH_DISPLAY` adds `retirement.fire_bridge_nis`, `concentration.us_situs_estate_exposure_nis`, `fx.usd_nis`; the canonical path also emits `fx.usd_nis_band_low/high`, `spend.mc_central_nis`/`mc_stress_nis`, `statutory.*`, `mc.*`, `concentration.nvda_analyst_floor_pct`, and DYNAMIC `allocation.<slug>` keys that cannot be statically enumerated. So `owner_for(key)` must combine an explicit map with **prefix rules** that own whole namespaces, and never hard-fail on an unknown key — it returns a best-rule owner and a flag so a completeness test catches genuinely-unowned keys.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_figure_registry.py
from argosy.quality.figure_registry import OWNER_MAP, OwnerSpec, owner_for


def test_explicit_owner_map_entry():
    spec = owner_for("retirement.fi_target_nis")
    assert spec.owner is OwnerRole.RETIREMENT_FI
    assert spec.kind is FigureKind.FORMULA_RESULT
    assert spec.materiality is Materiality.HIGH


def test_shared_concept_has_consult_set():
    spec = owner_for("concentration.nvda_cap_pct")
    assert spec.owner is OwnerRole.INVESTMENT
    assert OwnerRole.TAX in spec.consult


def test_prefix_rules_cover_dynamic_and_canonical_keys():
    # dynamic allocation.* and the canonical-path keys resolve by prefix rule.
    assert owner_for("allocation.global_equity").owner is OwnerRole.INVESTMENT
    assert owner_for("fx.usd_nis_band_low").owner is OwnerRole.BALANCE_SHEET
    assert owner_for("statutory.retirement_age").owner is OwnerRole.RETIREMENT_FI
    assert owner_for("mc.solvency_horizon_age").owner is OwnerRole.RETIREMENT_FI
    assert owner_for("spend.mc_central_nis").owner is OwnerRole.CASH_FLOW


def test_unknown_key_is_flagged_not_crashed():
    spec = owner_for("totally.unknown_key")
    assert spec.owner is OwnerRole.LEAD_PLANNER  # safe fallback
    assert spec.uncategorized is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -k "owner" -v`
Expected: FAIL with `ImportError: cannot import name 'OWNER_MAP'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to argosy/quality/figure_registry.py
@dataclass(frozen=True)
class OwnerSpec:
    owner: OwnerRole
    kind: FigureKind
    materiality: Materiality = Materiality.MEDIUM
    consult: tuple[OwnerRole, ...] = ()
    basis: str | None = None
    uncategorized: bool = False


_R, _T, _I, _B, _C = (
    OwnerRole.RETIREMENT_FI, OwnerRole.TAX, OwnerRole.INVESTMENT,
    OwnerRole.BALANCE_SHEET, OwnerRole.CASH_FLOW,
)
_FR, _AS, _MP = FigureKind.FORMULA_RESULT, FigureKind.ASSUMPTION, FigureKind.MODEL_PROJECTION
_HI, _MED, _LO = Materiality.HIGH, Materiality.MEDIUM, Materiality.LOW

# Explicit per-key owners (override prefix rules).
OWNER_MAP: dict[str, OwnerSpec] = {
    "portfolio.net_worth_nis": OwnerSpec(_B, _FR, _HI, basis="investable"),
    "portfolio.liquid_net_worth_nis": OwnerSpec(_B, _FR, _HI, basis="liquid"),
    "portfolio.usd_exposure_nis": OwnerSpec(_B, _FR, _MED),
    "retirement.fi_target_nis": OwnerSpec(_R, _FR, _HI),
    "retirement.fi_total_capital_nis": OwnerSpec(_R, _FR, _HI),
    "retirement.fi_margin_signed_nis": OwnerSpec(_R, _FR, _HI),
    "retirement.fire_bridge_nis": OwnerSpec(_R, _FR, _MED),
    "retirement.fi_age": OwnerSpec(_R, _MP, _HI),
    "retirement.earliest_safe_age": OwnerSpec(_R, _MP, _HI),
    "retirement.preservation_age": OwnerSpec(_R, _MP, _MED),
    "retirement.pension_unlock_age": OwnerSpec(_R, _AS, _LO),
    "retirement.mc_horizon_age": OwnerSpec(_R, _AS, _LO),
    "retirement.required_real_yield_pct": OwnerSpec(_R, _AS, _HI),
    "retirement.return_assumption_pct": OwnerSpec(_I, _AS, _HI),
    "retirement.liquidity_reserve_nis": OwnerSpec(_R, _FR, _MED),
    "spend.fi_basis_nis": OwnerSpec(_C, _FR, _HI),
    "savings.annual_net_nis": OwnerSpec(_C, _FR, _HI),
    "spend.annual_t12_nis": OwnerSpec(_C, _FR, _MED),
    "concentration.nvda_cap_pct": OwnerSpec(_I, _FR, _HI, consult=(_T, _R)),
    "concentration.nvda_target_pct": OwnerSpec(_I, _AS, _HI, consult=(_T, _R)),
    "concentration.nvda_current_pct": OwnerSpec(_I, _FR, _MED),
    "concentration.nvda_target_sh": OwnerSpec(_I, _FR, _MED, consult=(_T,)),
    "concentration.nvda_sell_sh": OwnerSpec(_I, _FR, _MED, consult=(_T,)),
    "concentration.nvda_eligible_now_sh": OwnerSpec(_T, _FR, _MED),
    "concentration.nvda_analyst_floor_pct": OwnerSpec(_I, _FR, _MED),
    "concentration.us_situs_estate_exposure_nis": OwnerSpec(OwnerRole.ESTATE, _FR, _HI),
    "spend.mc_central_nis": OwnerSpec(_C, _MP, _HI),
    "spend.mc_stress_nis": OwnerSpec(_C, _MP, _MED),
}

# Prefix rules own whole namespaces (incl. dynamic keys). First match wins.
_PREFIX_RULES: tuple[tuple[str, OwnerSpec], ...] = (
    ("allocation.", OwnerSpec(_I, _FR, _MED)),
    ("concentration.", OwnerSpec(_I, _FR, _MED)),
    ("fx.", OwnerSpec(_B, _FR, _MED)),
    ("statutory.", OwnerSpec(_R, _AS, _LO)),
    ("mc.", OwnerSpec(_R, _AS, _LO)),
    ("spend.", OwnerSpec(_C, _FR, _MED)),
    ("savings.", OwnerSpec(_C, _FR, _MED)),
    ("retirement.", OwnerSpec(_R, _FR, _MED)),
    ("portfolio.", OwnerSpec(_B, _FR, _MED)),
    ("tax.", OwnerSpec(_T, _AS, _MED)),
)


def owner_for(key: str) -> OwnerSpec:
    """Resolve a figure-id to its OwnerSpec: explicit OWNER_MAP first, then a
    prefix rule, else a flagged fallback (LEAD_PLANNER, uncategorized=True) so a
    completeness test catches a genuinely un-owned key — never a hard crash."""
    spec = OWNER_MAP.get(key)
    if spec is not None:
        return spec
    for prefix, rule in _PREFIX_RULES:
        if key.startswith(prefix):
            return rule
    return OwnerSpec(OwnerRole.LEAD_PLANNER, FigureKind.FORMULA_RESULT,
                     Materiality.MEDIUM, uncategorized=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -k "owner" -v`
Expected: PASS (all four)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/figure_registry.py tests/test_figure_registry.py
git commit -m "feat(registry): owner_for with explicit OWNER_MAP + prefix rules"
```

---

### Task 4: `validate_figure` — materiality-gated, pending-vs-blocked, fail-closed

**Files:**
- Modify: `argosy/quality/figure_registry.py`
- Test: `tests/test_figure_registry.py`

**Context (codex review):** Phase 1a has no cross-model validator. So: `formula_result`/`source_fact` resolve on `validated_by in {"resolver","recompute"}`. A judgment kind with NO evidence → `blocked` (fail-closed, a judgment must have a basis). With evidence + `LOW` materiality → `resolved`. With evidence + `HIGH/MEDIUM` + cross-model → `resolved`. With evidence + `HIGH/MEDIUM` + no cross-model → **`pending`** (awaiting Phase 3), NOT blocked. Value `None` → `pending`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_figure_registry.py
from argosy.quality.figure_registry import validate_figure


def _rec(**kw):
    base = dict(id="x", value=1.0, unit="nis",
                kind=FigureKind.FORMULA_RESULT, owner=OwnerRole.RETIREMENT_FI)
    base.update(kw)
    return FigureRecord(**base)


def test_formula_result_resolves_on_resolver_or_recompute():
    assert validate_figure(_rec(validated_by="resolver")).status == "resolved"
    assert validate_figure(_rec(validated_by="recompute")).status == "resolved"


def test_formula_result_pending_without_marker():
    assert validate_figure(_rec(validated_by="none")).status == "pending"


def test_source_fact_resolves_on_resolver():
    out = validate_figure(_rec(kind=FigureKind.SOURCE_FACT, validated_by="resolver"))
    assert out.status == "resolved"


def test_material_judgment_no_evidence_is_blocked():
    out = validate_figure(_rec(kind=FigureKind.RECOMMENDATION,
                               materiality=Materiality.HIGH))
    assert out.status == "blocked"


def test_material_judgment_with_evidence_no_cross_model_is_pending():
    for mat in (Materiality.HIGH, Materiality.MEDIUM):
        out = validate_figure(_rec(kind=FigureKind.MODEL_PROJECTION,
                                   materiality=mat, evidence=("src:1",)))
        assert out.status == "pending", mat


def test_material_judgment_with_cross_model_resolves():
    out = validate_figure(_rec(kind=FigureKind.RECOMMENDATION,
                               materiality=Materiality.HIGH, evidence=("src:1",),
                               validated_by="cross_model_rederivation"))
    assert out.status == "resolved"


def test_low_materiality_judgment_resolves_on_evidence():
    out = validate_figure(_rec(kind=FigureKind.ASSUMPTION,
                               materiality=Materiality.LOW, evidence=("src:1",)))
    assert out.status == "resolved"


def test_low_materiality_judgment_no_evidence_is_blocked():
    out = validate_figure(_rec(kind=FigureKind.ASSUMPTION,
                               materiality=Materiality.LOW))
    assert out.status == "blocked"


def test_none_value_stays_pending():
    assert validate_figure(_rec(value=None, validated_by="resolver")).status == "pending"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -k "validate_figure or judgment or formula_result or source_fact or none_value" -v`
Expected: FAIL with `ImportError: cannot import name 'validate_figure'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to argosy/quality/figure_registry.py
_DETERMINISTIC_KINDS = {FigureKind.FORMULA_RESULT, FigureKind.SOURCE_FACT}
_DETERMINISTIC_CLEARANCE = {"resolver", "recompute"}


def validate_figure(record: FigureRecord) -> FigureRecord:
    """Return ``record`` with ``status`` set by the materiality-gated rules.

    None value -> pending. Deterministic kinds -> resolved when cleared by the
    resolver/recompute, else pending. Judgment kinds: no evidence -> blocked
    (fail-closed); evidence + LOW -> resolved; evidence + HIGH/MEDIUM + cross-model
    -> resolved; evidence + HIGH/MEDIUM without cross-model -> pending (awaiting
    the Phase-3 cross-model validator), never silently resolved."""
    if record.value is None:
        return dataclasses.replace(record, status="pending")

    if record.kind in _DETERMINISTIC_KINDS:
        status = "resolved" if record.validated_by in _DETERMINISTIC_CLEARANCE else "pending"
        return dataclasses.replace(record, status=status)

    # judgment kinds
    if not record.evidence:
        return dataclasses.replace(record, status="blocked")
    if record.materiality is Materiality.LOW:
        return dataclasses.replace(record, status="resolved")
    status = "resolved" if record.validated_by == "cross_model_rederivation" else "pending"
    return dataclasses.replace(record, status=status)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -k "validate_figure or judgment or formula_result or source_fact or none_value" -v`
Expected: PASS (all nine)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/figure_registry.py tests/test_figure_registry.py
git commit -m "feat(registry): materiality-gated validate_figure (pending vs blocked)"
```

---

### Task 5: `build_figure_registry` — wrap a resolver manifest into records

**Files:**
- Modify: `argosy/quality/figure_registry.py`
- Test: `tests/test_figure_registry.py`

**Context:** `build_figure_registry(resolved, *, today=None)` walks `resolved.values`, resolves each key via `owner_for`, stamps `validated_by="resolver"` for deterministic kinds (the resolver computed them deterministically — an honest, weaker marker than a true raw-source recompute, which Phase 1c/3 adds), carries `source_locator` as evidence + `formula` as method + the as-of `today`, then runs `validate_figure`. Judgment kinds get `validated_by="none"` (so material ones land `pending` for Phase 3).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_figure_registry.py
from argosy.quality.figure_registry import build_figure_registry
from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers, ResolvedValue


def test_build_registry_wraps_with_ownership_and_resolves_formula():
    man = ResolvedPlanNumbers(values={
        "retirement.fi_target_nis": ResolvedValue(
            key="retirement.fi_target_nis", value=11_836_133.0, unit="nis",
            status="resolved", source_locator="fi_methodology", formula="spend/SWR"),
        "concentration.nvda_cap_pct": ResolvedValue(
            key="concentration.nvda_cap_pct", value=0.13, unit="pct",
            status="resolved", source_locator="target_allocation_doc"),
    })
    reg = build_figure_registry(man, today="2026-06-19")
    fi = reg["retirement.fi_target_nis"]
    assert fi.owner is OwnerRole.RETIREMENT_FI and fi.kind is FigureKind.FORMULA_RESULT
    assert fi.value == 11_836_133.0 and fi.evidence == ("fi_methodology",)
    assert fi.method == "spend/SWR" and fi.as_of == "2026-06-19"
    assert fi.validated_by == "resolver" and fi.status == "resolved"
    assert OwnerRole.TAX in reg["concentration.nvda_cap_pct"].consult


def test_build_registry_material_projection_is_pending():
    man = ResolvedPlanNumbers(values={
        "retirement.earliest_safe_age": ResolvedValue(
            key="retirement.earliest_safe_age", value=46.0, unit="age",
            status="resolved", source_locator="canonical_dual_track"),
    })
    rec = build_figure_registry(man)["retirement.earliest_safe_age"]
    assert rec.kind is FigureKind.MODEL_PROJECTION and rec.materiality is Materiality.HIGH
    assert rec.status == "pending"  # awaits Phase-3 cross-model validation


def test_build_registry_pending_value_stays_pending():
    man = ResolvedPlanNumbers(values={
        "retirement.fi_target_nis": ResolvedValue.pending(
            "retirement.fi_target_nis", "nis", "no source"),
    })
    assert build_figure_registry(man)["retirement.fi_target_nis"].status == "pending"


def test_build_registry_dynamic_allocation_key_is_owned():
    man = ResolvedPlanNumbers(values={
        "allocation.global_equity": ResolvedValue(
            key="allocation.global_equity", value=0.35, unit="pct",
            status="resolved", source_locator="target_allocation_doc"),
    })
    rec = build_figure_registry(man)["allocation.global_equity"]
    assert rec.owner is OwnerRole.INVESTMENT and rec.status == "resolved"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -k build_registry -v`
Expected: FAIL with `ImportError: cannot import name 'build_figure_registry'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to argosy/quality/figure_registry.py
def build_figure_registry(resolved, *, today: str | None = None) -> dict[str, FigureRecord]:
    """Wrap a ``ResolvedPlanNumbers`` manifest into validated ``FigureRecord``s.

    Each value is annotated from ``owner_for`` (owner/kind/materiality/consult/
    basis). Deterministic kinds are stamped ``validated_by="resolver"`` (the
    resolver's deterministic computation — Phase 1c/3 upgrades this to a true
    raw-source recompute); judgment kinds get ``"none"`` so material ones land
    ``pending`` for the Phase-3 cross-model validator. ``owner_for`` never raises,
    so no produced key crashes the build; an uncategorized key is owned by the
    Lead with ``uncategorized=True`` (caught by the coverage test)."""
    out: dict[str, FigureRecord] = {}
    for key, rv in resolved.values.items():
        spec = owner_for(key)
        validated_by = "resolver" if spec.kind in _DETERMINISTIC_KINDS else "none"
        loc = getattr(rv, "source_locator", None)
        rec = FigureRecord(
            id=key,
            value=rv.value,
            unit=rv.unit,
            kind=spec.kind,
            owner=spec.owner,
            consult=spec.consult,
            basis=spec.basis,
            method=getattr(rv, "formula", None),
            evidence=(loc,) if loc else (),
            confidence=getattr(rv, "confidence", None),
            materiality=spec.materiality,
            validated_by=validated_by,
            as_of=today,
        )
        out[key] = validate_figure(rec)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -k build_registry -v`
Expected: PASS (all four)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/figure_registry.py tests/test_figure_registry.py
git commit -m "feat(registry): build_figure_registry wraps resolver manifest"
```

---

### Task 6: Coverage over the REAL resolver output + exports + live smoke

**Files:**
- Modify: `argosy/quality/__init__.py`
- Test: `tests/test_figure_registry.py`

**Context (codex review):** the strongest coverage test runs the real resolver and asserts every produced key is owned (not `uncategorized`). This catches a future new resolver key that has neither an explicit entry nor a prefix rule. It needs a DB session; gate it so it skips cleanly if the dev DB is absent.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_figure_registry.py
import pytest


def test_static_owner_map_keys_are_all_categorized():
    # every explicit key resolves to itself (not the uncategorized fallback)
    for key in OWNER_MAP:
        assert owner_for(key).uncategorized is False


def test_synth_display_keys_are_owned():
    from argosy.services.plan_numeric_resolver import _SYNTH_DISPLAY
    bad = [k for (k, _label) in _SYNTH_DISPLAY if owner_for(k).uncategorized]
    assert bad == [], f"_SYNTH_DISPLAY keys with no owner: {bad}"


def test_live_resolver_keys_all_owned_and_no_blocked_formula():
    """Run the real resolver; every produced key must be owned (not uncategorized),
    and no formula_result/source_fact may be 'blocked'. Material judgments may be
    'pending' (awaiting Phase-3 cross-model). Skips if the dev DB is absent."""
    import os
    os.environ["ARGOSY_INCREMENTAL_PLAN"] = "1"
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from argosy.config import get_settings
        from argosy.services.plan_numeric_resolver import resolve_plan_numbers
    except Exception:
        pytest.skip("resolver deps unavailable")
    url = get_settings().database_url.replace("+aiosqlite", "")
    try:
        S = sessionmaker(bind=create_engine(url, connect_args={"check_same_thread": False}))
        with S() as s:
            man = resolve_plan_numbers(s, user_id="ariel", decision_run_id=117,
                                       include_canonical_ages=True)
    except Exception:
        pytest.skip("dev DB / run 117 unavailable")
    reg = build_figure_registry(man)
    uncategorized = sorted(k for k, r in reg.items()
                           if owner_for(k).uncategorized)
    assert uncategorized == [], f"un-owned resolver keys: {uncategorized}"
    blocked_formula = sorted(
        k for k, r in reg.items()
        if r.status == "blocked" and r.kind in (FigureKind.FORMULA_RESULT, FigureKind.SOURCE_FACT))
    assert blocked_formula == [], f"deterministic figures blocked: {blocked_formula}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -k "synth_display or live_resolver or categorized" -v`
Expected: FAIL — `test_synth_display_keys_are_owned` may report keys still missing a prefix/explicit owner (add them to `OWNER_MAP` or a prefix rule until it passes); the live test should then pass or skip.

- [ ] **Step 3: Fix any gaps surfaced**

If `test_synth_display_keys_are_owned` or the live test lists un-owned keys, add each to `OWNER_MAP` (Task 3) with the correct owner, or extend `_PREFIX_RULES`. Re-run until both pass.

- [ ] **Step 4: Add exports**

```python
# in argosy/quality/__init__.py — add to imports + __all__
from argosy.quality.figure_registry import (
    FigureKind, FigureRecord, Materiality, OwnerRole, OwnerSpec,
    OWNER_MAP, owner_for, build_figure_registry, validate_figure,
)
# append those names to the module's __all__ list.
```

- [ ] **Step 5: Run the whole file + the public-export check**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -v`
Expected: PASS (live test PASS or SKIP). Also confirm `from argosy.quality import build_figure_registry` imports.

- [ ] **Step 6: Commit**

```bash
git add argosy/quality/figure_registry.py argosy/quality/__init__.py tests/test_figure_registry.py
git commit -m "feat(registry): real-resolver coverage tests + package exports"
```

---

## Self-Review

- **Spec coverage:** Phase 1a = spec Phase-1 items 1 (FigureRecord full shape incl. `timestamp`), 2 (owner map + consult/sign-off + prefix-rule coverage of dynamic keys), 5 (materiality-gated publish status). Items 3 (new canonical figures + Data-Steward freshness) and 4 (hot-surface render cutover) are explicit follow-on plans 1b/1c — stated in Scope. The plan no longer claims Phase-1-complete (codex finding #1/#7).
- **Codex findings addressed:** #2 prefix-rule coverage + `_SYNTH_DISPLAY` + live-resolver coverage tests; #3 corrected smoke — deterministic `blocked: []`, material judgments `pending` not blocked; #4 honest `validated_by="resolver"` marker (not "recompute") with a note that 1c/3 upgrades to raw-source recompute; #5 `timestamp` added + `as_of` populated (full Data-Steward freshness deferred to 1b, stated); #6 stronger tests (medium materiality, source_fact, unknown-key fallback, dynamic keys, live resolver). 
- **Placeholder scan:** none; every code step shows complete code; every command shows expected output.
- **Type consistency:** `FigureKind`/`Materiality`/`OwnerRole`/`OwnerSpec`/`FigureRecord`/`OWNER_MAP`/`owner_for`/`validate_figure`/`build_figure_registry` used with identical signatures across Tasks 1–6; `validated_by` values `{none, resolver, recompute, cross_model_rederivation}` consistent.
- **No new derivation math:** confirmed — annotation + validation only.
