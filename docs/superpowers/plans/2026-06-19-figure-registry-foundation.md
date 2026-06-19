# Canonical Figure Registry — Foundation (Phase 1a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the canonical figure registry that wraps every resolved plan figure with an owner, a kind, semantic identity, materiality, evidence, and a fail-closed validation status — the spine that makes "one owner per figure" enforceable.

**Architecture:** A new pure module `argosy/quality/figure_registry.py` defines `FigureKind`, `Materiality`, `OwnerRole` enums, the `FigureRecord` dataclass, a static `OWNER_MAP` (figure-id → owner + consult set + kind + materiality + basis), `build_figure_registry(resolved)` that wraps a `ResolvedPlanNumbers` manifest into `FigureRecord`s, and `validate_figure(record)` that enforces evidence-per-kind, materiality-gated. No new derivation math, no LLM, no DB — it annotates the existing resolver output. This is the foundation for Phase 1b (adding the missing canonical figures) and Phase 2 (render-from-registry).

**Tech Stack:** Python 3.12, dataclasses + `enum.Enum`, pytest. Mirrors the existing `argosy/quality/gate_types.py` (dataclasses, str-enums, pure, no persistence).

**Scope note:** This plan is the registry FOUNDATION only (spec §"Phase 1" items 1, 2, 5 over existing figures). Adding the new canonical figures (`net_worth.total_incl_residence_nis`, `retirement.fi_crossing_year`, the retention split, pool/slice — item 3) and cutting the contradiction-prone surfaces to registry rendering (item 4) are dependent follow-on plans (Phase 1b / 1c) that build on this module. Spec: `docs/superpowers/specs/2026-06-19-financial-advisory-team-design.md`.

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
    # Owner roles are the generalized firm roster (not client-specific).
    assert OwnerRole.RETIREMENT_FI.value == "retirement_fi"
    assert OwnerRole.LEAD_PLANNER.value == "lead_planner"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py::test_enums_have_expected_members -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'argosy.quality.figure_registry'`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/quality/figure_registry.py
"""Canonical figure registry (Phase 1a foundation).

Wraps the deterministic resolver output (``ResolvedPlanNumbers``) with the
ownership + classification + evidence + validation metadata that makes
"one accountable owner per figure" enforceable. Pure: no DB, no LLM, no new
derivation math — it annotates figures the resolver already produced. See
docs/superpowers/specs/2026-06-19-financial-advisory-team-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FigureKind(str, Enum):
    """How a figure is produced — sets the validation it must clear."""

    SOURCE_FACT = "source_fact"          # extracted from a document/feed
    ASSUMPTION = "assumption"            # a policy/parameter (SWR, return)
    FORMULA_RESULT = "formula_result"    # deterministic math over inputs
    MODEL_PROJECTION = "model_projection"  # stochastic/MC output
    INTERPRETATION = "interpretation"    # legal/tax judgment
    RECOMMENDATION = "recommendation"    # an advised action/target


class Materiality(str, Enum):
    """How much validation a figure must clear before publish."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class OwnerRole(str, Enum):
    """The generalized firm roster — each the single Responsible owner of its
    figures. Client-agnostic (no instrument/employer specifics)."""

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

### Task 2: `FigureRecord` dataclass

**Files:**
- Modify: `argosy/quality/figure_registry.py`
- Test: `tests/test_figure_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_figure_registry.py
from argosy.quality.figure_registry import FigureRecord


def test_figure_record_carries_full_identity_and_defaults():
    r = FigureRecord(
        id="retirement.fi_target_nis",
        value=11_836_133.0,
        unit="nis",
        kind=FigureKind.FORMULA_RESULT,
        owner=OwnerRole.RETIREMENT_FI,
    )
    # semantic-identity + lifecycle fields default safely
    assert r.basis is None and r.scenario is None and r.as_of is None
    assert r.materiality is Materiality.MEDIUM   # default
    assert r.consult == ()                       # no consult set by default
    assert r.evidence == ()
    assert r.validated_by == "none"
    assert r.status == "pending"                 # not validated yet
    assert r.version == 0
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
    # consulted owners with a required sign-off on changes to a shared concept.
    consult: tuple[OwnerRole, ...] = ()
    # --- semantic identity: a single ID is not enough; two surfaces can
    #     "agree" on a number while meaning different things ---
    basis: str | None = None            # liquid | investable | total | ...
    scenario: str | None = None         # baseline | stress | MC regime
    as_of: str | None = None            # date/state the value is true for
    jurisdiction: str | None = None
    policy_version: str | None = None
    precision: str | None = None
    # --- provenance / evidence / lifecycle ---
    inputs: tuple[str, ...] = ()
    method: str | None = None
    evidence: tuple[str, ...] = ()
    source_freshness: str | None = None
    confidence: str | None = None
    materiality: Materiality = Materiality.MEDIUM
    validated_by: str = "none"          # none | recompute | cross_model_rederivation
    status: str = "pending"             # pending | resolved | blocked
    version: int = 0
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

### Task 3: `OWNER_MAP` + completeness over the resolver keys

**Files:**
- Modify: `argosy/quality/figure_registry.py`
- Test: `tests/test_figure_registry.py`

**Context:** The resolver's canonical keys live in `argosy/services/plan_numeric_resolver.py::_KEY_UNITS` plus the keys added in `_SYNTH_DISPLAY`. Every key a surface can show must have exactly one Responsible owner. This task asserts completeness so a future new resolver key cannot ship un-owned.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_figure_registry.py
from argosy.quality.figure_registry import OWNER_MAP, OwnerSpec


def test_owner_map_assigns_one_owner_with_kind_and_materiality():
    spec = OWNER_MAP["retirement.fi_target_nis"]
    assert isinstance(spec, OwnerSpec)
    assert spec.owner is OwnerRole.RETIREMENT_FI
    assert spec.kind is FigureKind.FORMULA_RESULT
    assert spec.materiality is Materiality.HIGH   # load-bearing headline


def test_owner_map_covers_every_resolver_key():
    from argosy.services.plan_numeric_resolver import _KEY_UNITS
    missing = sorted(k for k in _KEY_UNITS if k not in OWNER_MAP)
    assert missing == [], f"resolver keys with no owner: {missing}"


def test_shared_concept_has_consult_set():
    # NVDA cap is owned by Investment but Tax + Retirement are consulted.
    spec = OWNER_MAP["concentration.nvda_cap_pct"]
    assert spec.owner is OwnerRole.INVESTMENT
    assert OwnerRole.TAX in spec.consult
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -k owner_map -v`
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


_R, _T, _I, _B, _C = (
    OwnerRole.RETIREMENT_FI, OwnerRole.TAX, OwnerRole.INVESTMENT,
    OwnerRole.BALANCE_SHEET, OwnerRole.CASH_FLOW,
)
_FR, _AS, _MP = FigureKind.FORMULA_RESULT, FigureKind.ASSUMPTION, FigureKind.MODEL_PROJECTION
_HI, _MED = Materiality.HIGH, Materiality.MEDIUM

# figure-id -> who is Responsible, its kind, materiality, consult set, basis.
# Mirrors plan_numeric_resolver._KEY_UNITS; every key there MUST appear here
# (enforced by test_owner_map_covers_every_resolver_key).
OWNER_MAP: dict[str, OwnerSpec] = {
    "portfolio.net_worth_nis": OwnerSpec(_B, _FR, _HI, basis="investable"),
    "portfolio.liquid_net_worth_nis": OwnerSpec(_B, _FR, _HI, basis="liquid"),
    "portfolio.usd_exposure_nis": OwnerSpec(_B, _FR, _MED),
    "retirement.fi_target_nis": OwnerSpec(_R, _FR, _HI),
    "retirement.fi_total_capital_nis": OwnerSpec(_R, _FR, _HI),
    "retirement.fi_margin_signed_nis": OwnerSpec(_R, _FR, _HI),
    "retirement.fi_age": OwnerSpec(_R, _MP, _HI),
    "retirement.earliest_safe_age": OwnerSpec(_R, _MP, _HI),
    "retirement.preservation_age": OwnerSpec(_R, _MP, _MED),
    "retirement.pension_unlock_age": OwnerSpec(_R, _AS, _MED),
    "retirement.mc_horizon_age": OwnerSpec(_R, _AS, _MED),
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
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -k owner_map -v`
Expected: PASS (all three). If `test_owner_map_covers_every_resolver_key` fails, add the missing key(s) to `OWNER_MAP` with the right owner.

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/figure_registry.py tests/test_figure_registry.py
git commit -m "feat(registry): OWNER_MAP with completeness over resolver keys"
```

---

### Task 4: `validate_figure` — materiality-gated, fail-closed

**Files:**
- Modify: `argosy/quality/figure_registry.py`
- Test: `tests/test_figure_registry.py`

**Context:** A figure may only reach `status="resolved"` if it carries the evidence its kind requires, scaled by materiality. `formula_result` clears on `validated_by="recompute"`. A `materiality=high` judgment kind (`assumption`/`model_projection`/`interpretation`/`recommendation`) needs evidence AND `validated_by="cross_model_rederivation"`. A `materiality=low` figure needs evidence but not the cross-model check. A missing requirement → `status="blocked"` (fail-closed), never `resolved`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_figure_registry.py
from argosy.quality.figure_registry import validate_figure


def _rec(**kw):
    base = dict(id="x", value=1.0, unit="nis",
                kind=FigureKind.FORMULA_RESULT, owner=OwnerRole.RETIREMENT_FI)
    base.update(kw)
    return FigureRecord(**base)


def test_formula_result_resolves_on_recompute():
    out = validate_figure(_rec(kind=FigureKind.FORMULA_RESULT, validated_by="recompute"))
    assert out.status == "resolved"


def test_formula_result_blocked_without_recompute():
    out = validate_figure(_rec(kind=FigureKind.FORMULA_RESULT, validated_by="none"))
    assert out.status == "blocked"


def test_material_judgment_needs_evidence_and_cross_model():
    base = dict(kind=FigureKind.RECOMMENDATION, materiality=Materiality.HIGH)
    # missing both -> blocked
    assert validate_figure(_rec(**base)).status == "blocked"
    # evidence but no cross-model -> still blocked (fail-closed)
    assert validate_figure(_rec(evidence=("src:1",), **base)).status == "blocked"
    # both present -> resolved
    ok = validate_figure(_rec(evidence=("src:1",),
                              validated_by="cross_model_rederivation", **base))
    assert ok.status == "resolved"


def test_low_materiality_judgment_needs_evidence_not_cross_model():
    out = validate_figure(_rec(kind=FigureKind.ASSUMPTION,
                               materiality=Materiality.LOW, evidence=("src:1",)))
    assert out.status == "resolved"


def test_pending_value_stays_pending():
    out = validate_figure(_rec(value=None, validated_by="recompute"))
    assert out.status == "pending"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -k validate_figure -v`
Expected: FAIL with `ImportError: cannot import name 'validate_figure'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to argosy/quality/figure_registry.py
import dataclasses

_JUDGMENT_KINDS = {
    FigureKind.ASSUMPTION, FigureKind.MODEL_PROJECTION,
    FigureKind.INTERPRETATION, FigureKind.RECOMMENDATION,
}


def validate_figure(record: FigureRecord) -> FigureRecord:
    """Return ``record`` with ``status`` set by the materiality-gated rules.
    Fail-closed: any unmet requirement -> ``blocked`` (never ``resolved``).
    A figure with no value stays ``pending`` (nothing to validate yet)."""
    if record.value is None:
        return dataclasses.replace(record, status="pending")

    status = "blocked"
    if record.kind in (FigureKind.FORMULA_RESULT, FigureKind.SOURCE_FACT):
        # deterministic / extracted: cleared by recompute against the source.
        if record.validated_by == "recompute":
            status = "resolved"
    else:  # judgment kinds
        has_evidence = bool(record.evidence)
        if record.materiality is Materiality.LOW:
            status = "resolved" if has_evidence else "blocked"
        else:  # high / medium material judgment -> needs blind cross-model check
            if has_evidence and record.validated_by == "cross_model_rederivation":
                status = "resolved"
    return dataclasses.replace(record, status=status)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -k validate_figure -v`
Expected: PASS (all five)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/figure_registry.py tests/test_figure_registry.py
git commit -m "feat(registry): materiality-gated fail-closed validate_figure"
```

---

### Task 5: `build_figure_registry` — wrap a resolver manifest into records

**Files:**
- Modify: `argosy/quality/figure_registry.py`
- Test: `tests/test_figure_registry.py`

**Context:** `build_figure_registry(resolved)` walks a `ResolvedPlanNumbers` manifest, and for each value found, builds a `FigureRecord` using `OWNER_MAP` for owner/kind/materiality/consult/basis, carrying the resolver's `value/unit/source_locator/formula/confidence`. It marks `validated_by="recompute"` for `formula_result`/`source_fact` (the resolver IS the deterministic recompute) so those resolve; judgment kinds are left for the cross-model pass (Phase 3) and so resolve only when low-materiality-with-evidence. A resolver key absent from `OWNER_MAP` is a hard error (caught earlier by the completeness test, but defended here too).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_figure_registry.py
from argosy.quality.figure_registry import build_figure_registry
from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers, ResolvedValue


def test_build_registry_wraps_resolved_values_with_ownership():
    man = ResolvedPlanNumbers(values={
        "retirement.fi_target_nis": ResolvedValue(
            key="retirement.fi_target_nis", value=11_836_133.0, unit="nis",
            status="resolved", source_locator="fi_methodology"),
        "concentration.nvda_cap_pct": ResolvedValue(
            key="concentration.nvda_cap_pct", value=0.13, unit="pct",
            status="resolved", source_locator="target_allocation_doc"),
    })
    reg = build_figure_registry(man)
    fi = reg["retirement.fi_target_nis"]
    assert fi.owner is OwnerRole.RETIREMENT_FI
    assert fi.kind is FigureKind.FORMULA_RESULT
    assert fi.value == 11_836_133.0
    assert fi.evidence == ("fi_methodology",)
    # formula_result is recompute-cleared by the resolver -> resolved
    assert fi.status == "resolved"
    # shared-concept consult carried through
    assert OwnerRole.TAX in reg["concentration.nvda_cap_pct"].consult


def test_build_registry_marks_pending_when_value_missing():
    man = ResolvedPlanNumbers(values={
        "retirement.fi_target_nis": ResolvedValue.pending(
            "retirement.fi_target_nis", "nis", "no source"),
    })
    reg = build_figure_registry(man)
    assert reg["retirement.fi_target_nis"].status == "pending"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -k build_registry -v`
Expected: FAIL with `ImportError: cannot import name 'build_figure_registry'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to argosy/quality/figure_registry.py
def build_figure_registry(resolved) -> dict[str, FigureRecord]:
    """Wrap a ``ResolvedPlanNumbers`` manifest into validated ``FigureRecord``s.

    Each resolved value is annotated from ``OWNER_MAP`` (owner/kind/materiality/
    consult/basis). ``formula_result``/``source_fact`` figures are treated as
    recompute-cleared (the resolver is the deterministic recompute); judgment
    kinds carry their evidence and are validated per materiality. Unknown keys
    (absent from OWNER_MAP) raise — every published figure must have an owner."""
    out: dict[str, FigureRecord] = {}
    for key, rv in resolved.values.items():
        spec = OWNER_MAP.get(key)
        if spec is None:
            raise KeyError(f"figure {key!r} has no owner in OWNER_MAP")
        validated_by = "recompute" if spec.kind in (
            FigureKind.FORMULA_RESULT, FigureKind.SOURCE_FACT) else "none"
        rec = FigureRecord(
            id=key,
            value=rv.value,
            unit=rv.unit,
            kind=spec.kind,
            owner=spec.owner,
            consult=spec.consult,
            basis=spec.basis,
            inputs=(),
            method=getattr(rv, "formula", None),
            evidence=(rv.source_locator,) if rv.source_locator else (),
            confidence=getattr(rv, "confidence", None),
            materiality=spec.materiality,
            validated_by=validated_by,
        )
        out[key] = validate_figure(rec)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -k build_registry -v`
Expected: PASS (both)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/figure_registry.py tests/test_figure_registry.py
git commit -m "feat(registry): build_figure_registry wraps resolver manifest"
```

---

### Task 6: Live smoke + export the module surface

**Files:**
- Modify: `argosy/quality/__init__.py` (export the public names)
- Test: `tests/test_figure_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_figure_registry.py
def test_public_exports_available_from_quality_package():
    from argosy.quality import (
        FigureRecord, FigureKind, Materiality, OwnerRole,
        OWNER_MAP, build_figure_registry, validate_figure,
    )
    assert FigureKind.FORMULA_RESULT.value == "formula_result"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py::test_public_exports_available_from_quality_package -v`
Expected: FAIL with `ImportError: cannot import name 'FigureRecord' from 'argosy.quality'`

- [ ] **Step 3: Write minimal implementation**

```python
# in argosy/quality/__init__.py — add to the imports + __all__
from argosy.quality.figure_registry import (
    FigureKind, FigureRecord, Materiality, OwnerRole, OwnerSpec,
    OWNER_MAP, build_figure_registry, validate_figure,
)
# ... and append those names to the module's __all__ list.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_figure_registry.py -v`
Expected: PASS (whole file)

- [ ] **Step 5: Live smoke against the real resolver (read-only)**

Run:
```bash
.venv/Scripts/python.exe - <<'PY'
import os
os.environ["ARGOSY_INCREMENTAL_PLAN"] = "1"
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from argosy.config import get_settings
from argosy.services.plan_numeric_resolver import resolve_plan_numbers
from argosy.quality.figure_registry import build_figure_registry
url = get_settings().database_url.replace("+aiosqlite", "")
S = sessionmaker(bind=create_engine(url, connect_args={"check_same_thread": False}))
with S() as s:
    man = resolve_plan_numbers(s, user_id="ariel", decision_run_id=117, include_canonical_ages=True)
    reg = build_figure_registry(man)
    print("figures:", len(reg))
    print("un-owned KeyErrors: none (would have raised)")
    blocked = [k for k, r in reg.items() if r.status == "blocked"]
    print("blocked:", blocked)
PY
```
Expected: prints a figure count > 0, no `KeyError` (every resolved key is owned), and `blocked: []` (every resolved formula figure clears recompute). If a key raises `KeyError`, add it to `OWNER_MAP` (Task 3) and re-run.

- [ ] **Step 6: Commit**

```bash
git add argosy/quality/__init__.py tests/test_figure_registry.py
git commit -m "feat(registry): export registry surface from argosy.quality + live smoke"
```

---

## Self-Review

- **Spec coverage:** This plan covers Phase 1 items 1 (FigureRecord shape), 2 (owner map + consult/sign-off), and 5 (materiality-gated publish per figure) over the EXISTING resolver figures. Item 3 (new canonical figures: `net_worth.total_incl_residence_nis`, `retirement.fi_crossing_year`, retention split, pool/slice) and item 4 (cut the contradiction-prone surfaces to registry rendering) are explicitly deferred to follow-on plans 1b/1c that import this module — noted in the Scope note.
- **Placeholder scan:** no TBD/TODO; every code step shows complete code; every command shows expected output.
- **Type consistency:** `FigureKind`, `Materiality`, `OwnerRole`, `OwnerSpec`, `FigureRecord`, `OWNER_MAP`, `validate_figure`, `build_figure_registry` are used with identical signatures across Tasks 1-6.
- **No new derivation math:** confirmed — the registry only annotates + validates resolver output (spec non-goal honored).
