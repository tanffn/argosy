# Coherence Deliberation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the oscillating reader→closer loop with a role-based coherence mechanism that conforms every surface of a contradiction (markdown + structured JSON), records durable machine-checkable rulings, routes goal tensions to an arbitrator, and gates promotion on a deterministic verifier — closing draft 45 as the acceptance case.

**Architecture:** A pipeline invoked when the whole-artifact reader BLOCKs: cluster findings into structured-identity *disputes* → route value mismatches to a deterministic **resolver** and goal/framing tensions to a **panel → facilitator → coherence_arbitrator** → a **conformer** applies typed all-surface patches → a **ledger** persists ruling + a typed `coherence_invariant` → a deterministic **verifier** gates promotion before a re-read that may only appeal (`new_dispute`/`ruling_divergence`/`ruling_defect`). Fail-closed = BLOCK everywhere.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic, Pydantic v2, pytest. Agents subclass `BaseAgent[T]`. Reference spec: `docs/superpowers/specs/2026-06-17-coherence-deliberation-arbitrator-roles-design.md`.

---

## Conventions (read once)

- Python interpreter: `D:/Projects/financial-advisor/.venv/Scripts/python.exe`. Run tests with `-m "not llm_eval"`.
- Console is cp1252 — never print ₪/Hebrew; tests compare against escaped or ASCII strings, and any probe writes UTF-8 files.
- Agent tests MUST mock the model call (no live `claude.exe`). Pattern: monkeypatch the agent's `run_sync`/`run` to return a stub `AgentReport(output=<model instance>)`, or call `build_prompt` directly and assert on the prompt + feed a hand-built output model to downstream code. Never let a test reach `_call_model`.
- New code lives under `argosy/quality/coherence/` (pure logic) and `argosy/agents/` (LLM roles). The orchestrating loop lives in `argosy/orchestrator/flows/plan_synthesis/coherence_deliberation.py`.
- Commit after every task with the trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## File structure (created/modified)

**Slice 1 — deterministic substrate**
- Create `argosy/quality/coherence/__init__.py` — package marker.
- Create `argosy/quality/coherence/dispute.py` — `Dispute` (structured identity), `dispute_key()`, finding→dispute clusterer.
- Create `argosy/quality/coherence/surface_registry.py` — `SurfaceSite` + `SUBJECT_REGISTRY` (subject → surfaces, field paths, conform method, derived deps).
- Create `argosy/quality/coherence/invariants.py` — typed `Invariant` classes + `verify_invariants()`.
- Create `argosy/quality/coherence/conformer.py` — `ConformPatch`, `apply_patches()` (atomic across md + json, number-boundary guard, idempotent).
- Create `argosy/quality/coherence/resolver_route.py` — `route_dispute()` + `build_value_patch()` (deterministic value mismatches).

**Slice 2 — framing contract**
- Create `argosy/quality/coherence/claim_markers.py` — render/parse typed claim markers in markdown.
- Extend `invariants.py` — `RequiredFramingRole`, `ForbiddenClaim`, `SurfaceClaimEquals`.

**Slice 3 — ledger**
- Modify `argosy/state/models.py` — add `CoherenceDecision`.
- Create `alembic/versions/0026_coherence_decisions.py` — migration.
- Create `argosy/quality/coherence/ledger.py` — persist/supersede/load helpers.

**Slice 4 — reader appeal**
- Modify `argosy/orchestrator/flows/plan_synthesis/whole_artifact_reader.py` — typed finding fields + appeal kinds + ledger injection into the prompt.

**Slice 5 — panel/arbitrator roles**
- Create `argosy/agents/coherence_panelist.py`, `argosy/agents/coherence_facilitator.py`, `argosy/agents/coherence_arbitrator.py`.
- Create `argosy/orchestrator/flows/plan_synthesis/coherence_deliberation.py` — the orchestrating loop.
- Modify `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` — invoke deliberation in place of the blind closer.

**Slice 6 — expansion + e2e**
- Extend `SUBJECT_REGISTRY` to all draft-45 subjects; add the deliberation telemetry appendix to `plan_export.py`; draft-45 e2e acceptance test.

Tests live under `tests/coherence/` mirroring the module names.

---

## Slice 1 — Deterministic substrate (registry, resolver route, conformer, verifier)

### Task 1.1: Package + structured Dispute identity

**Files:**
- Create: `argosy/quality/coherence/__init__.py`
- Create: `argosy/quality/coherence/dispute.py`
- Test: `tests/coherence/test_dispute.py`

- [ ] **Step 1: Create the package marker**

```python
# argosy/quality/coherence/__init__.py
"""Coherence deliberation: cluster reader findings into structured disputes,
resolve/arbitrate, conform every surface, verify, and persist durable rulings."""
```

- [ ] **Step 2: Write the failing test**

```python
# tests/coherence/test_dispute.py
from argosy.quality.coherence.dispute import Dispute, dispute_key


def test_dispute_key_is_stable_across_question_phrasing():
    a = Dispute(
        subject_type="retirement_age_headline",
        subject_field_path="retirement.earliest_safe_age",
        scope="person",
        conflict_type="policy_tension",
        normalized_options=("age_46_typical", "age_54_preservation"),
        implicated_canonical_fact_ids=("retirement.earliest_safe_age",),
        implicated_user_directive_ids=("prime_directive", "capital_preservation_style"),
        question="Which retirement age leads?",
    )
    b = Dispute(
        subject_type="retirement_age_headline",
        subject_field_path="retirement.earliest_safe_age",
        scope="person",
        conflict_type="policy_tension",
        # options listed in a DIFFERENT order, question reworded
        normalized_options=("age_54_preservation", "age_46_typical"),
        implicated_canonical_fact_ids=("retirement.earliest_safe_age",),
        implicated_user_directive_ids=("capital_preservation_style", "prime_directive"),
        question="Is 46 or 54 the binding headline?",
    )
    assert dispute_key(a) == dispute_key(b)


def test_dispute_key_differs_on_subject():
    a = Dispute(subject_type="rsu_vest_policy", subject_field_path="", scope="person",
                conflict_type="value_mismatch", normalized_options=(), question="x")
    b = Dispute(subject_type="sgln_ucits_membership", subject_field_path="", scope="person",
                conflict_type="value_mismatch", normalized_options=(), question="y")
    assert dispute_key(a) != dispute_key(b)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_dispute.py -v`
Expected: FAIL with `ModuleNotFoundError: argosy.quality.coherence.dispute`

- [ ] **Step 4: Implement `dispute.py`**

```python
# argosy/quality/coherence/dispute.py
"""Structured dispute identity. The dispute_key is a hash over STRUCTURED fields
(never the natural-language question), computed AFTER normalization so phrasing
drift cannot mint a new key. Surface IDs are evidence, not identity."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

ConflictType = Literal[
    "value_mismatch", "policy_tension", "calc_inconsistency", "representation_mismatch"
]


@dataclass(frozen=True)
class Dispute:
    subject_type: str
    subject_field_path: str
    scope: str
    conflict_type: ConflictType
    normalized_options: tuple[str, ...] = ()
    implicated_canonical_fact_ids: tuple[str, ...] = ()
    implicated_user_directive_ids: tuple[str, ...] = ()
    question: str = ""  # human-readable; NOT part of identity
    surfaces_cited: tuple[str, ...] = ()  # evidence; NOT part of identity


def dispute_key(d: Dispute) -> str:
    """Stable identity hash over normalized structured fields only."""
    parts = [
        d.subject_type.strip().lower(),
        d.subject_field_path.strip().lower(),
        d.scope.strip().lower(),
        d.conflict_type,
        "|".join(sorted(o.strip().lower() for o in d.normalized_options)),
        "|".join(sorted(f.strip().lower() for f in d.implicated_canonical_fact_ids)),
        "|".join(sorted(x.strip().lower() for x in d.implicated_user_directive_ids)),
    ]
    raw = "\x1f".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_dispute.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add argosy/quality/coherence/__init__.py argosy/quality/coherence/dispute.py tests/coherence/test_dispute.py
git commit -m "feat(coherence): structured Dispute identity + stable dispute_key"
```

### Task 1.2: Surface registry

**Files:**
- Create: `argosy/quality/coherence/surface_registry.py`
- Test: `tests/coherence/test_surface_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_surface_registry.py
from argosy.quality.coherence.surface_registry import (
    SurfaceSite, sites_for_subject, SUBJECT_REGISTRY,
)


def test_rsu_vest_subject_has_md_and_json_surfaces():
    sites = sites_for_subject("rsu_vest_policy")
    assert sites, "rsu_vest_policy must be registered"
    methods = {s.conform_method for s in sites}
    # the vest policy renders in markdown bodies AND the action JSON
    assert "markdown" in methods
    assert "json_field" in methods


def test_every_site_names_its_surface_and_path():
    for subject, sites in SUBJECT_REGISTRY.items():
        for s in sites:
            assert isinstance(s, SurfaceSite)
            assert s.surface_id and s.conform_method in {"markdown", "json_field", "derived"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_surface_registry.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `surface_registry.py`**

```python
# argosy/quality/coherence/surface_registry.py
"""Declarative map: subject_type -> the surfaces it renders on, each with a field
path and a conform method. Names every place a fact appears so the conformer can
reach ALL of them and the verifier can assert coverage. `derived_from` lists sites
whose value is computed from another, so a conform refreshes dependents.

Seeds the draft-45 subjects; extended in Slice 6. Surface ids:
  long_md / medium_md / short_md          -> PlanVersion.horizon_*_md (markdown)
  short_actions_json / medium_actions_json -> PlanVersion.horizon_*_json (actions[])
  dashboard.<field>                        -> computed WealthDashboard field (derived)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ConformMethod = Literal["markdown", "json_field", "derived"]


@dataclass(frozen=True)
class SurfaceSite:
    subject_type: str
    surface_id: str
    field_path: str = ""           # JSON path for json_field; section anchor for markdown
    conform_method: ConformMethod = "markdown"
    derived_from: tuple[str, ...] = ()  # surface_ids this site is computed from


SUBJECT_REGISTRY: dict[str, list[SurfaceSite]] = {
    "rsu_vest_policy": [
        SurfaceSite("rsu_vest_policy", "long_md", "equity_comp", "markdown"),
        SurfaceSite("rsu_vest_policy", "medium_md", "themes.nvda_rsu", "markdown"),
        SurfaceSite("rsu_vest_policy", "short_md", "posture", "markdown"),
        SurfaceSite("rsu_vest_policy", "short_actions_json",
                    "$.actions[?label~='RSU vest']", "json_field"),
    ],
    "sgln_ucits_membership": [
        SurfaceSite("sgln_ucits_membership", "medium_md", "themes.sgln", "markdown"),
        SurfaceSite("sgln_ucits_membership", "short_md", "posture", "markdown"),
        SurfaceSite("sgln_ucits_membership", "short_actions_json",
                    "$.actions[?label~='UCITS dollar-cost']", "json_field"),
    ],
    "retirement_age_headline": [
        SurfaceSite("retirement_age_headline", "long_md", "reconciliation", "markdown"),
        SurfaceSite("retirement_age_headline", "long_md", "monte_carlo", "markdown"),
        SurfaceSite("retirement_age_headline", "long_md", "withdrawal", "markdown"),
        SurfaceSite("retirement_age_headline", "long_md", "client_goals", "markdown"),
        SurfaceSite("retirement_age_headline", "medium_md", "targets", "markdown"),
    ],
    "tranche_execution_gate": [
        SurfaceSite("tranche_execution_gate", "short_md", "tax_plan", "markdown"),
        SurfaceSite("tranche_execution_gate", "short_md", "actions", "markdown"),
        SurfaceSite("tranche_execution_gate", "short_actions_json",
                    "$.actions[?label~='NVDA June tranche']", "json_field"),
    ],
}


def sites_for_subject(subject_type: str) -> list[SurfaceSite]:
    return list(SUBJECT_REGISTRY.get(subject_type, []))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_surface_registry.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/coherence/surface_registry.py tests/coherence/test_surface_registry.py
git commit -m "feat(coherence): surface registry (subject -> all render surfaces + conform method)"
```

### Task 1.3: Value invariants + verifier core

**Files:**
- Create: `argosy/quality/coherence/invariants.py`
- Test: `tests/coherence/test_invariants.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_invariants.py
from argosy.quality.coherence.invariants import (
    EqualsCanonical, AllRegisteredSurfacesPresent, verify_invariants, VerifyResult,
)


def test_equals_canonical_passes_when_all_surfaces_match():
    artifact = {"long_md": "NVDA cap is 13.0%", "short_md": "the 13.0% NVDA ceiling"}
    inv = EqualsCanonical(
        subject_type="nvda_cap", canonical_text="13.0",
        surfaces=("long_md", "short_md"),
    )
    res = verify_invariants([inv], artifact)
    assert res.ok is True
    assert res.failures == []


def test_equals_canonical_fails_when_a_surface_diverges():
    artifact = {"long_md": "NVDA cap is 13.0%", "short_md": "the 12.0% NVDA ceiling"}
    inv = EqualsCanonical(
        subject_type="nvda_cap", canonical_text="13.0",
        surfaces=("long_md", "short_md"),
    )
    res = verify_invariants([inv], artifact)
    assert res.ok is False
    assert any("short_md" in f for f in res.failures)


def test_all_surfaces_present_fails_when_one_missing():
    artifact = {"long_md": "x"}  # short_md absent
    inv = AllRegisteredSurfacesPresent(subject_type="vest", surfaces=("long_md", "short_md"))
    res = verify_invariants([inv], artifact)
    assert res.ok is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_invariants.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `invariants.py` (value classes + verifier)**

```python
# argosy/quality/coherence/invariants.py
"""Typed, code-evaluable invariants over named surfaces. The verifier gates
MECHANICAL compliance only (numbers/fields/required+forbidden typed claims/
coverage). It never attempts semantic-truth checking of prose — that is the
reader-appeal layer's job. Framing-contract invariants are added in Slice 2.

`artifact` is a dict[surface_id -> str] (markdown bodies) plus, for json_field
surfaces, the verifier is given parsed claim text by the conformer (Slice 2 adds
claim markers; Slice 1 value checks operate on rendered text)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class VerifyResult:
    ok: bool
    failures: list[str] = field(default_factory=list)


class Invariant(Protocol):
    def check(self, artifact: dict[str, str]) -> list[str]:
        """Return a list of failure messages (empty == satisfied)."""
        ...


@dataclass(frozen=True)
class EqualsCanonical:
    subject_type: str
    canonical_text: str
    surfaces: tuple[str, ...]

    def check(self, artifact: dict[str, str]) -> list[str]:
        out: list[str] = []
        for s in self.surfaces:
            text = artifact.get(s)
            if text is None:
                out.append(f"{self.subject_type}: surface {s} absent")
            elif self.canonical_text not in text:
                out.append(
                    f"{self.subject_type}: surface {s} does not state "
                    f"canonical '{self.canonical_text}'"
                )
        return out


@dataclass(frozen=True)
class AllRegisteredSurfacesPresent:
    subject_type: str
    surfaces: tuple[str, ...]

    def check(self, artifact: dict[str, str]) -> list[str]:
        return [
            f"{self.subject_type}: registered surface {s} missing"
            for s in self.surfaces
            if artifact.get(s) is None
        ]


def verify_invariants(invariants: list[Invariant], artifact: dict[str, str]) -> VerifyResult:
    failures: list[str] = []
    for inv in invariants:
        failures.extend(inv.check(artifact))
    return VerifyResult(ok=not failures, failures=failures)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_invariants.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/coherence/invariants.py tests/coherence/test_invariants.py
git commit -m "feat(coherence): value invariants (equals_canonical, surfaces_present) + verifier core"
```

### Task 1.4: Conformer (atomic patch apply, number-boundary guard, idempotent)

**Files:**
- Create: `argosy/quality/coherence/conformer.py`
- Test: `tests/coherence/test_conformer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_conformer.py
import json
from argosy.quality.coherence.conformer import ConformPatch, apply_patches, ConformResult


def test_markdown_patch_applies_and_is_idempotent():
    bodies = {"long_md": "retain net vested as NVDA", "medium_md": "", "short_md": ""}
    patch = ConformPatch(
        surface_id="long_md", conform_method="markdown",
        find="retain net vested as NVDA", replace="sell net vested NVDA -> SGOV",
    )
    res = apply_patches(bodies, {}, [patch])
    assert res.ok
    assert "sell net vested NVDA -> SGOV" in res.bodies["long_md"]
    # idempotent: re-applying the same patch is a no-op success (find already gone)
    res2 = apply_patches(res.bodies, {}, [patch])
    assert res2.ok
    assert res2.bodies["long_md"] == res.bodies["long_md"]


def test_json_field_patch_sets_action_detail():
    actions = {"actions": [{"label": "UCITS dollar-cost tranche",
                            "detail": "split across CSPX/FUSA/EIMI/SGLN"}]}
    patch = ConformPatch(
        surface_id="short_actions_json", conform_method="json_field",
        match_label="UCITS dollar-cost", set_field="detail",
        new_value="split across CSPX/FUSA/EIMI only; SGLN standalone",
    )
    res = apply_patches({}, {"short_actions_json": actions}, [patch])
    assert res.ok
    assert "SGLN standalone" in res.json_surfaces["short_actions_json"]["actions"][0]["detail"]


def test_number_boundary_guard_rejects_fabricated_number():
    bodies = {"long_md": "earliest-safe age 46", "medium_md": "", "short_md": ""}
    patch = ConformPatch(
        surface_id="long_md", conform_method="markdown",
        find="earliest-safe age 46", replace="earliest-safe age 51",  # invented
    )
    res = apply_patches(bodies, {}, [patch], allowed_numbers=frozenset({"46", "54", "44"}))
    assert res.ok is False
    assert res.bodies == bodies  # no partial application
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_conformer.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `conformer.py`**

```python
# argosy/quality/coherence/conformer.py
"""Apply a typed patch plan across ALL surfaces atomically. Builds the full new
state first; if any patch is unsafe or unapplicable, returns ok=False with NO
mutation (callers must BLOCK). Number-boundary guard rejects a replacement that
introduces a numeric token not present in `find` or `allowed_numbers`. Markdown
patches are idempotent (a find already absent is a satisfied no-op)."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

_NUM = re.compile(r"\d[\d,\.]*")


@dataclass(frozen=True)
class ConformPatch:
    surface_id: str
    conform_method: str                 # "markdown" | "json_field"
    # markdown:
    find: str = ""
    replace: str = ""
    # json_field:
    match_label: str = ""               # substring match on actions[].label
    set_field: str = ""                 # e.g. "detail" | "label"
    new_value: str = ""


@dataclass
class ConformResult:
    ok: bool
    bodies: dict[str, str] = field(default_factory=dict)
    json_surfaces: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _numbers(text: str) -> set[str]:
    return {m.group(0).rstrip(".").replace(",", "") for m in _NUM.finditer(text)}


def _edit_introduces_number(find: str, replace: str, allowed: frozenset[str]) -> bool:
    introduced = _numbers(replace) - _numbers(find) - {n.replace(",", "") for n in allowed}
    return bool(introduced)


def apply_patches(
    bodies: dict[str, str],
    json_surfaces: dict[str, Any],
    patches: list[ConformPatch],
    *,
    allowed_numbers: frozenset[str] = frozenset(),
) -> ConformResult:
    new_bodies = dict(bodies)
    new_json = copy.deepcopy(json_surfaces)
    errors: list[str] = []

    for p in patches:
        if p.conform_method == "markdown":
            text = new_bodies.get(p.surface_id, "")
            if p.replace and _edit_introduces_number(p.find, p.replace, allowed_numbers):
                errors.append(f"{p.surface_id}: replacement introduces a fabricated number")
                continue
            if p.find and p.find in text:
                new_bodies[p.surface_id] = text.replace(p.find, p.replace, 1)
            elif p.find and p.replace and p.replace in text:
                pass  # idempotent: already conformed
            elif p.find:
                errors.append(f"{p.surface_id}: find text not present and not already conformed")
        elif p.conform_method == "json_field":
            surface = new_json.get(p.surface_id)
            if not isinstance(surface, dict):
                errors.append(f"{p.surface_id}: json surface missing")
                continue
            hits = [a for a in surface.get("actions") or []
                    if isinstance(a, dict) and p.match_label in (a.get("label") or "")]
            if len(hits) != 1:
                # idempotent acceptance: if already set, treat as satisfied
                already = [a for a in surface.get("actions") or []
                           if isinstance(a, dict) and p.new_value in (a.get(p.set_field) or "")]
                if not already:
                    errors.append(f"{p.surface_id}: expected 1 action for '{p.match_label}', got {len(hits)}")
                continue
            hits[0][p.set_field] = p.new_value
        else:
            errors.append(f"{p.surface_id}: unknown conform_method {p.conform_method}")

    if errors:
        return ConformResult(ok=False, bodies=dict(bodies),
                             json_surfaces=copy.deepcopy(json_surfaces), errors=errors)
    return ConformResult(ok=True, bodies=new_bodies, json_surfaces=new_json)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_conformer.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/coherence/conformer.py tests/coherence/test_conformer.py
git commit -m "feat(coherence): atomic all-surface conformer (md + json), number-boundary guard, idempotent"
```

### Task 1.5: Resolver route (deterministic value mismatches)

**Files:**
- Create: `argosy/quality/coherence/resolver_route.py`
- Test: `tests/coherence/test_resolver_route.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_resolver_route.py
from argosy.quality.coherence.dispute import Dispute
from argosy.quality.coherence.resolver_route import route_dispute, RouteKind


def test_value_mismatch_routes_to_resolver():
    d = Dispute(subject_type="nvda_cap", subject_field_path="concentration.nvda_cap_pct",
                scope="person", conflict_type="value_mismatch", question="x")
    assert route_dispute(d) == RouteKind.RESOLVER


def test_policy_tension_routes_to_arbitration():
    d = Dispute(subject_type="retirement_age_headline", subject_field_path="",
                scope="person", conflict_type="policy_tension", question="x")
    assert route_dispute(d) == RouteKind.ARBITRATION


def test_untypeable_routes_to_block():
    d = Dispute(subject_type="", subject_field_path="", scope="",
                conflict_type="value_mismatch", question="x")
    assert route_dispute(d) == RouteKind.BLOCK
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_resolver_route.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `resolver_route.py`**

```python
# argosy/quality/coherence/resolver_route.py
"""Deterministic routing: value/calc mismatches with a canonical source go to the
resolver; representation mismatches with a canonical render go to the resolver too;
policy/framing tensions go to arbitration; anything un-typeable BLOCKs."""
from __future__ import annotations

import enum

from argosy.quality.coherence.dispute import Dispute


class RouteKind(enum.Enum):
    RESOLVER = "resolver"
    ARBITRATION = "arbitration"
    BLOCK = "block"


def route_dispute(d: Dispute) -> RouteKind:
    if not d.subject_type:
        return RouteKind.BLOCK
    if d.conflict_type in ("value_mismatch", "calc_inconsistency", "representation_mismatch"):
        # representational/value disputes are conformable from canonical source
        return RouteKind.RESOLVER
    if d.conflict_type == "policy_tension":
        return RouteKind.ARBITRATION
    return RouteKind.BLOCK
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_resolver_route.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/coherence/resolver_route.py tests/coherence/test_resolver_route.py
git commit -m "feat(coherence): deterministic dispute router (resolver / arbitration / block)"
```

---

## Slice 2 — Framing contract (typed claim markers + framing invariants)

Framing disputes (the retirement-age case) have no numeric invariant. They are
gated by a **framing contract**: typed claim markers the render emits alongside
prose, plus `RequiredFramingRole` / `ForbiddenClaim` / `SurfaceClaimEquals`
invariants the verifier evaluates over the parsed markers (not the prose).

### Task 2.1: Typed claim markers (render + parse)

**Files:**
- Create: `argosy/quality/coherence/claim_markers.py`
- Test: `tests/coherence/test_claim_markers.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_claim_markers.py
from argosy.quality.coherence.claim_markers import (
    render_marker, parse_markers, strip_markers,
)


def test_render_and_parse_roundtrip():
    m = render_marker("retirement_age_headline", {"lead_age": "46", "strict_track_age": "54"})
    text = f"Some prose about retirement. {m}\nMore prose."
    claims = parse_markers(text)
    assert claims["retirement_age_headline"]["lead_age"] == "46"
    assert claims["retirement_age_headline"]["strict_track_age"] == "54"


def test_strip_markers_removes_them_for_human_reading():
    m = render_marker("rsu_vest_policy", {"action": "sell_to_sgov"})
    text = f"Sell net vested NVDA. {m}"
    assert "sell_to_sgov" not in strip_markers(text)
    assert "Sell net vested NVDA." in strip_markers(text)


def test_parse_returns_empty_when_no_markers():
    assert parse_markers("plain prose") == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_claim_markers.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `claim_markers.py`**

```python
# argosy/quality/coherence/claim_markers.py
"""Typed claim markers: a machine-readable claim block embedded in markdown as an
HTML comment so it is invisible in rendered prose but deterministically parseable.
The verifier reads markers, never the prose. Markers are stripped from the
reader-facing artifact AND can be stripped for a clean human read.

Marker form:  <!--coh:subject_type k1=v1;k2=v2-->
"""
from __future__ import annotations

import re

_MARKER = re.compile(r"<!--coh:(?P<subj>[a-z0-9_]+)\s+(?P<body>[^>]*?)-->")


def render_marker(subject_type: str, claims: dict[str, str]) -> str:
    body = ";".join(f"{k}={v}" for k, v in claims.items())
    return f"<!--coh:{subject_type} {body}-->"


def parse_markers(text: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for m in _MARKER.finditer(text or ""):
        claims: dict[str, str] = {}
        for pair in m.group("body").split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                claims[k.strip()] = v.strip()
        out[m.group("subj")] = claims
    return out


def strip_markers(text: str) -> str:
    return _MARKER.sub("", text or "").rstrip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_claim_markers.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/coherence/claim_markers.py tests/coherence/test_claim_markers.py
git commit -m "feat(coherence): typed claim markers (machine-readable, prose-invisible)"
```

### Task 2.2: Framing-contract invariants

**Files:**
- Modify: `argosy/quality/coherence/invariants.py`
- Test: `tests/coherence/test_framing_invariants.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_framing_invariants.py
from argosy.quality.coherence.claim_markers import render_marker
from argosy.quality.coherence.invariants import (
    RequiredFramingRole, ForbiddenClaim, verify_invariants,
)


def _artifact(**markers):
    # one surface per kw, each carrying a single subject marker
    return {sid: render_marker(subj, claims) for sid, (subj, claims) in markers.items()}


def test_required_framing_role_passes_when_marker_present():
    art = _artifact(long_md=("retirement_age_headline",
                             {"lead_age": "46", "capital_preservation_role": "target_sizing_basis"}))
    inv = RequiredFramingRole(
        subject_type="retirement_age_headline", surface="long_md",
        role_field="lead_age", value="46",
    )
    assert verify_invariants([inv], art).ok


def test_required_framing_role_fails_on_wrong_value():
    art = _artifact(long_md=("retirement_age_headline", {"lead_age": "54"}))
    inv = RequiredFramingRole(
        subject_type="retirement_age_headline", surface="long_md",
        role_field="lead_age", value="46",
    )
    res = verify_invariants([inv], art)
    assert not res.ok and any("lead_age" in f for f in res.failures)


def test_forbidden_claim_fails_when_pattern_present_in_prose():
    art = {"short_md": "we will retain net vested as NVDA until the cap-band fires"}
    inv = ForbiddenClaim(subject_type="rsu_vest_policy", surface="short_md",
                         pattern="retain net vested as NVDA")
    res = verify_invariants([inv], art)
    assert not res.ok
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_framing_invariants.py -v`
Expected: FAIL with `ImportError: cannot import name 'RequiredFramingRole'`

- [ ] **Step 3: Add framing invariants to `invariants.py`**

Append these classes to `argosy/quality/coherence/invariants.py` (after `AllRegisteredSurfacesPresent`). Add `from argosy.quality.coherence.claim_markers import parse_markers` at the top of the file.

```python
@dataclass(frozen=True)
class RequiredFramingRole:
    """A surface's typed claim marker must carry role_field == value."""
    subject_type: str
    surface: str
    role_field: str
    value: str

    def check(self, artifact: dict[str, str]) -> list[str]:
        text = artifact.get(self.surface)
        if text is None:
            return [f"{self.subject_type}: surface {self.surface} absent"]
        claims = parse_markers(text).get(self.subject_type, {})
        actual = claims.get(self.role_field)
        if actual != self.value:
            return [
                f"{self.subject_type}: {self.surface} framing {self.role_field}="
                f"{actual!r}, expected {self.value!r}"
            ]
        return []


@dataclass(frozen=True)
class ForbiddenClaim:
    """A surface's PROSE must not contain a forbidden substring (mechanical guard
    against a known-wrong claim, e.g. the retired 'retain as NVDA' policy)."""
    subject_type: str
    surface: str
    pattern: str

    def check(self, artifact: dict[str, str]) -> list[str]:
        text = artifact.get(self.surface) or ""
        if self.pattern in text:
            return [f"{self.subject_type}: {self.surface} contains forbidden claim '{self.pattern}'"]
        return []


@dataclass(frozen=True)
class SurfaceClaimEquals:
    """A typed claim block on a surface holds the expected value for claim_key."""
    subject_type: str
    surface: str
    claim_key: str
    value: str

    def check(self, artifact: dict[str, str]) -> list[str]:
        claims = parse_markers(artifact.get(self.surface) or "").get(self.subject_type, {})
        if claims.get(self.claim_key) != self.value:
            return [
                f"{self.subject_type}: {self.surface} claim {self.claim_key}="
                f"{claims.get(self.claim_key)!r}, expected {self.value!r}"
            ]
        return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_framing_invariants.py tests/coherence/test_invariants.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/coherence/invariants.py tests/coherence/test_framing_invariants.py
git commit -m "feat(coherence): framing-contract invariants (required_framing_role, forbidden_claim, surface_claim_equals)"
```

---

## Slice 3 — Ledger (model, migration, supersession)

### Task 3.1: `CoherenceDecision` model

**Files:**
- Modify: `argosy/state/models.py` (add model near the other plan-related models)
- Test: `tests/coherence/test_ledger_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_ledger_model.py
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from argosy.state.models import Base, CoherenceDecision


def _mem_session():
    eng = sa.create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_coherence_decision_persists_with_json_columns():
    s = _mem_session()
    row = CoherenceDecision(
        user_id="ariel", decision_run_id=109, dispute_key="abc123",
        subject_type="retirement_age_headline", question="which age leads?",
        ruling="age 46 leads; 54 strict track", rationale="prime directive",
        basis="prime_directive", resolved_by="arbitrator",
        coherence_invariant_json='[{"kind":"required_framing_role"}]',
        conformed_surfaces_json='["long_md","medium_md"]',
    )
    s.add(row); s.commit()
    got = s.query(CoherenceDecision).filter_by(dispute_key="abc123").one()
    assert got.resolved_by == "arbitrator"
    assert got.superseded_by_id is None
    assert got.created_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_ledger_model.py -v`
Expected: FAIL with `ImportError: cannot import name 'CoherenceDecision'`

- [ ] **Step 3: Add the model to `argosy/state/models.py`**

Find the existing `_utcnow` helper and `Base` (already defined). Add this class alongside the other plan models:

```python
class CoherenceDecision(Base):
    """A durable, machine-checkable coherence ruling. Versioned/supersedable:
    a replacement supersedes the prior row (which is retained for audit)."""

    __tablename__ = "coherence_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    dispute_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ruling: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    basis: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    resolved_by: Mapped[str] = mapped_column(String(16), nullable=False)  # resolver|consensus|arbitrator
    coherence_invariant_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    conformed_surfaces_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    superseded_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("coherence_decisions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_ledger_model.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/state/models.py tests/coherence/test_ledger_model.py
git commit -m "feat(state): CoherenceDecision model (durable, supersedable coherence rulings)"
```

### Task 3.2: Alembic migration 0026

**Files:**
- Create: `alembic/versions/0026_coherence_decisions.py`
- Test: `tests/coherence/test_migration_0026.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_migration_0026.py
import importlib.util
from pathlib import Path


def test_migration_0026_header_and_chains_from_0025():
    path = Path("alembic/versions/0026_coherence_decisions.py")
    assert path.exists()
    spec = importlib.util.spec_from_file_location("m0026", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert mod.revision == "0026_coherence_decisions"
    assert mod.down_revision == "0025_decision_phases_seq_unique"
    assert callable(mod.upgrade) and callable(mod.downgrade)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_migration_0026.py -v`
Expected: FAIL (file does not exist)

- [ ] **Step 3: Create the migration**

```python
# alembic/versions/0026_coherence_decisions.py
"""coherence_decisions ledger

Revision ID: 0026_coherence_decisions
Revises: 0025_decision_phases_seq_unique
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0026_coherence_decisions"
down_revision: str | None = "0025_decision_phases_seq_unique"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "coherence_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("decision_run_id", sa.Integer(), nullable=True),
        sa.Column("dispute_key", sa.String(length=32), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False, server_default=""),
        sa.Column("ruling", sa.Text(), nullable=False, server_default=""),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("basis", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("resolved_by", sa.String(length=16), nullable=False),
        sa.Column("coherence_invariant_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("conformed_surfaces_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("superseded_by_id", sa.Integer(), sa.ForeignKey("coherence_decisions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_coherence_decisions_user_id", "coherence_decisions", ["user_id"])
    op.create_index("ix_coherence_decisions_decision_run_id", "coherence_decisions", ["decision_run_id"])
    op.create_index("ix_coherence_decisions_dispute_key", "coherence_decisions", ["dispute_key"])


def downgrade() -> None:
    op.drop_index("ix_coherence_decisions_dispute_key", table_name="coherence_decisions")
    op.drop_index("ix_coherence_decisions_decision_run_id", table_name="coherence_decisions")
    op.drop_index("ix_coherence_decisions_user_id", table_name="coherence_decisions")
    op.drop_table("coherence_decisions")
```

- [ ] **Step 4: Run test + apply the migration**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_migration_0026.py -v`
Expected: PASS
Run: `.venv/Scripts/python.exe -m alembic upgrade head`
Expected: `Running upgrade 0025_decision_phases_seq_unique -> 0026_coherence_decisions`

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/0026_coherence_decisions.py tests/coherence/test_migration_0026.py
git commit -m "feat(db): migration 0026 — coherence_decisions ledger table"
```

### Task 3.3: Ledger helpers (persist, supersede, load-active)

**Files:**
- Create: `argosy/quality/coherence/ledger.py`
- Test: `tests/coherence/test_ledger.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_ledger.py
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from argosy.state.models import Base
from argosy.quality.coherence.ledger import record_ruling, load_active_rulings, supersede


def _s():
    eng = sa.create_engine("sqlite://"); Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_record_then_load_active():
    s = _s()
    record_ruling(s, user_id="ariel", decision_run_id=1, dispute_key="k1",
                  subject_type="rsu_vest_policy", question="q", ruling="sell->sgov",
                  rationale="deconcentration", basis="user_directive", resolved_by="resolver",
                  invariants=[{"kind": "forbidden_claim", "pattern": "retain"}],
                  conformed_surfaces=["short_md"])
    active = load_active_rulings(s, user_id="ariel")
    assert len(active) == 1 and active[0].dispute_key == "k1"


def test_supersede_keeps_old_for_audit_and_drops_from_active():
    s = _s()
    old = record_ruling(s, user_id="ariel", decision_run_id=1, dispute_key="k1",
                        subject_type="x", question="q", ruling="v1", rationale="r",
                        basis="b", resolved_by="resolver", invariants=[], conformed_surfaces=[])
    new = record_ruling(s, user_id="ariel", decision_run_id=2, dispute_key="k1",
                        subject_type="x", question="q", ruling="v2", rationale="r",
                        basis="b", resolved_by="arbitrator", invariants=[], conformed_surfaces=[])
    supersede(s, old_id=old.id, new_id=new.id)
    active = load_active_rulings(s, user_id="ariel")
    assert [r.ruling for r in active] == ["v2"]
    assert s.query(type(old)).count() == 2  # old retained for audit
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `ledger.py`**

```python
# argosy/quality/coherence/ledger.py
"""Persist / supersede / load coherence rulings. Active = not superseded. A
replacement supersedes the prior only AFTER it is written (callers conform+verify
before calling supersede), so the old invariant stays enforced until replaced."""
from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa

from argosy.state.models import CoherenceDecision


def record_ruling(
    session, *, user_id: str, decision_run_id: int | None, dispute_key: str,
    subject_type: str, question: str, ruling: str, rationale: str, basis: str,
    resolved_by: str, invariants: list[dict[str, Any]], conformed_surfaces: list[str],
) -> CoherenceDecision:
    row = CoherenceDecision(
        user_id=user_id, decision_run_id=decision_run_id, dispute_key=dispute_key,
        subject_type=subject_type, question=question, ruling=ruling, rationale=rationale,
        basis=basis, resolved_by=resolved_by,
        coherence_invariant_json=json.dumps(invariants, ensure_ascii=False),
        conformed_surfaces_json=json.dumps(conformed_surfaces, ensure_ascii=False),
    )
    session.add(row); session.commit()
    return row


def load_active_rulings(session, *, user_id: str) -> list[CoherenceDecision]:
    return list(
        session.execute(
            sa.select(CoherenceDecision).where(
                CoherenceDecision.user_id == user_id,
                CoherenceDecision.superseded_by_id.is_(None),
            ).order_by(CoherenceDecision.id)
        ).scalars()
    )


def supersede(session, *, old_id: int, new_id: int) -> None:
    row = session.get(CoherenceDecision, old_id)
    if row is not None:
        row.superseded_by_id = new_id
        session.commit()


def invariants_of(row: CoherenceDecision) -> list[dict[str, Any]]:
    try:
        return json.loads(row.coherence_invariant_json or "[]")
    except json.JSONDecodeError:
        return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_ledger.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/coherence/ledger.py tests/coherence/test_ledger.py
git commit -m "feat(coherence): ledger helpers (record / load-active / supersede)"
```

---

## Slice 4 — Reader appeal (typed findings + ledger injection)

The reader keeps its adversarial role but, given settled rulings, may only emit
`new_dispute`, `ruling_divergence`, or `ruling_defect` for arbitrated subjects — it
must not re-litigate a settled preference. This is the laundering guard (codex #1).

### Task 4.1: Extend the finding schema with structured fields + appeal kinds

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis/whole_artifact_reader.py`
- Test: `tests/coherence/test_reader_finding_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_reader_finding_schema.py
from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import CoherenceFinding


def test_finding_accepts_structured_fields_and_appeal_kinds():
    f = CoherenceFinding(
        kind="ruling_defect", severity="BLOCKER",
        detail="ruling uses a stale FX rate", surfaces_cited=["long_md"],
        subject_type="retirement_age_headline", field_path="retirement.earliest_safe_age",
        normalized_claim="age_54_leads",
    )
    assert f.kind == "ruling_defect"
    assert f.subject_type == "retirement_age_headline"


def test_structured_fields_default_empty_for_back_compat():
    f = CoherenceFinding(kind="contradiction", severity="AMBER",
                         detail="x", surfaces_cited=[])
    assert f.subject_type == ""
    assert f.normalized_claim == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_reader_finding_schema.py -v`
Expected: FAIL (`ruling_defect` not a valid kind / unexpected kwarg `subject_type`)

- [ ] **Step 3: Modify `CoherenceFinding` in `whole_artifact_reader.py`**

Extend the `kind` Literal to include the three appeal kinds and add three optional
structured fields (defaults keep all existing call sites valid):

```python
    kind: Literal[
        "contradiction", "cross_surface", "fragile_claim", "stale", "regression", "other",
        "new_dispute", "ruling_divergence", "ruling_defect",
    ]
    # ... existing severity / detail / surfaces_cited ...
    subject_type: str = ""
    field_path: str = ""
    normalized_claim: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_reader_finding_schema.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis/whole_artifact_reader.py tests/coherence/test_reader_finding_schema.py
git commit -m "feat(reader): typed finding fields + appeal kinds (new_dispute/ruling_divergence/ruling_defect)"
```

### Task 4.2: Inject settled rulings into the reader prompt

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis/whole_artifact_reader.py` (the
  prompt builder + `run_whole_artifact_review` signature gains `settled_rulings`)
- Test: `tests/coherence/test_reader_ruling_injection.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_reader_ruling_injection.py
from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    build_settled_rulings_block,
)


def test_ruling_block_lists_settled_and_states_appeal_contract():
    block = build_settled_rulings_block([
        {"subject_type": "retirement_age_headline",
         "ruling": "age 46 leads; 54 strict track; capital-preservation = target-sizing basis"},
    ])
    assert "retirement_age_headline" in block
    assert "age 46 leads" in block
    # contract language must instruct the reader NOT to re-litigate preference,
    # but to allow ruling_divergence / ruling_defect
    assert "ruling_divergence" in block
    assert "ruling_defect" in block


def test_empty_rulings_yields_empty_block():
    assert build_settled_rulings_block([]) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_reader_ruling_injection.py -v`
Expected: FAIL (`build_settled_rulings_block` undefined)

- [ ] **Step 3: Add `build_settled_rulings_block` + thread it into the prompt**

Add to `whole_artifact_reader.py`:

```python
def build_settled_rulings_block(settled_rulings: list[dict]) -> str:
    """Render the settled-ruling contract injected into the reader prompt."""
    if not settled_rulings:
        return ""
    lines = [
        "SETTLED RULINGS — these questions are arbitrated. Do NOT re-litigate the "
        "preferred answer. You MUST still verify every surface against the ruling. "
        "Emit `ruling_divergence` if a surface disagrees with a ruling; emit "
        "`ruling_defect` if a ruling itself is stale, overbroad, unsupported, wrongly "
        "scoped, or violates the authority order; emit `new_dispute` for anything not "
        "covered below.",
    ]
    for r in settled_rulings:
        lines.append(f"- [{r.get('subject_type','')}] {r.get('ruling','')}")
    return "\n".join(lines)
```

Then add a keyword-only `settled_rulings: list[dict] | None = None` param to
`run_whole_artifact_review(...)`, and in the body splice
`build_settled_rulings_block(settled_rulings or [])` into the user prompt (append it
to the existing `external_context` section so the reader sees it). Keep the default
`None` so existing callers are unaffected.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_reader_ruling_injection.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis/whole_artifact_reader.py tests/coherence/test_reader_ruling_injection.py
git commit -m "feat(reader): inject settled-ruling contract (appeal path, no preference relitigation)"
```

---

## Slice 5 — Panel, facilitator, arbitrator roles + orchestrating loop

### Task 5.1: `coherence_panelist` agent

**Files:**
- Create: `argosy/agents/coherence_panelist.py`
- Test: `tests/coherence/test_coherence_panelist.py`

- [ ] **Step 1: Write the failing test (build_prompt only; never call the model)**

```python
# tests/coherence/test_coherence_panelist.py
from argosy.agents.coherence_panelist import CoherencePanelistAgent, PanelistPosition


def test_build_prompt_includes_role_dispute_and_peer_positions():
    agent = CoherencePanelistAgent(user_id="ariel")
    system, user = agent.build_prompt(
        represented_role="withdrawal_sequencer",
        dispute_question="Which retirement age is the binding headline?",
        canonical_facts="earliest_safe_age=46; preservation_age=54",
        peer_positions=["equity perspective: capital-preservation style => 54"],
    )
    assert "withdrawal_sequencer" in user
    assert "binding headline" in user
    assert "54" in user
    assert agent.agent_role == "coherence_panelist"


def test_output_model_shape():
    p = PanelistPosition(position="age 46 leads", basis="prime_directive",
                         cites=["retirement.earliest_safe_age"])
    assert p.basis == "prime_directive"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_coherence_panelist.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `coherence_panelist.py`**

```python
# argosy/agents/coherence_panelist.py
"""A panelist representing one surface-owning role. States its position on a single
disputed question, grounded in the prime directive / user directives / canonical
facts, having seen the peers' positions. Pure opinion — it does not rule."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent


class PanelistPosition(BaseModel):
    position: str = Field(description="This role's position on the disputed question.")
    basis: Literal["prime_directive", "user_directive", "canonical_fact", "preference"] = Field(
        description="The highest-authority basis backing the position."
    )
    cites: list[str] = Field(default_factory=list, description="Canonical fact / directive ids.")
    concede: bool = Field(default=False, description="True if conceding to a peer position.")


class CoherencePanelistAgent(BaseAgent[PanelistPosition]):
    agent_role = "coherence_panelist"
    output_model = PanelistPosition
    require_citations = False

    def build_prompt(
        self, *, represented_role: str, dispute_question: str,
        canonical_facts: str, peer_positions: list[str],
    ) -> tuple[str, str]:
        system = (
            "You are a coherence-deliberation panelist representing the "
            f"'{represented_role}' perspective. State your position on ONE disputed "
            "question, grounded ONLY in the prime directive, the user's directives, or "
            "the canonical facts below. You may concede to a peer if their basis "
            "outranks yours. You do NOT rule — you argue your surface's view."
        )
        peers = "\n".join(f"  - {p}" for p in peer_positions) or "  (none yet)"
        user = (
            f"DISPUTED QUESTION:\n{dispute_question}\n\n"
            f"CANONICAL FACTS:\n{canonical_facts}\n\n"
            f"PEER POSITIONS:\n{peers}\n"
        )
        return system, user
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_coherence_panelist.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/coherence_panelist.py tests/coherence/test_coherence_panelist.py
git commit -m "feat(agents): coherence_panelist (role perspective on one dispute)"
```

### Task 5.2: `coherence_facilitator` agent

**Files:**
- Create: `argosy/agents/coherence_facilitator.py`
- Test: `tests/coherence/test_coherence_facilitator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_coherence_facilitator.py
from argosy.agents.coherence_facilitator import CoherenceFacilitatorAgent, FacilitatorOutcome


def test_build_prompt_lists_positions():
    agent = CoherenceFacilitatorAgent(user_id="ariel")
    system, user = agent.build_prompt(
        dispute_question="which age leads?",
        positions=[{"role": "withdrawal", "position": "46", "basis": "prime_directive"},
                   {"role": "goals", "position": "54", "basis": "user_directive"}],
    )
    assert "which age leads?" in user
    assert "withdrawal" in user and "goals" in user
    assert agent.agent_role == "coherence_facilitator"


def test_outcome_model():
    o = FacilitatorOutcome(consensus=False, ruling="", crux="prime vs stated style")
    assert o.consensus is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_coherence_facilitator.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `coherence_facilitator.py`**

```python
# argosy/agents/coherence_facilitator.py
"""Reads panelist positions; reports consensus + the agreed ruling, or no-consensus
+ the crux. Mirrors risk_facilitator. Does NOT impose a ruling on no-consensus —
that escalates to the arbitrator."""
from __future__ import annotations

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent


class FacilitatorOutcome(BaseModel):
    consensus: bool = Field(description="True if panelists agree.")
    ruling: str = Field(default="", description="The agreed ruling when consensus is True.")
    crux: str = Field(default="", description="The core disagreement when consensus is False.")


class CoherenceFacilitatorAgent(BaseAgent[FacilitatorOutcome]):
    agent_role = "coherence_facilitator"
    output_model = FacilitatorOutcome
    require_citations = False

    def build_prompt(self, *, dispute_question: str, positions: list[dict]) -> tuple[str, str]:
        system = (
            "You facilitate a coherence panel. Determine whether the panelists agree. "
            "If they do, state the agreed ruling. If not, state the crux of the "
            "disagreement crisply. Do NOT invent a ruling on no-consensus."
        )
        lines = "\n".join(
            f"  - {p.get('role','?')}: {p.get('position','')} (basis={p.get('basis','')})"
            for p in positions
        )
        user = f"DISPUTED QUESTION:\n{dispute_question}\n\nPOSITIONS:\n{lines}\n"
        return system, user
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_coherence_facilitator.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/coherence_facilitator.py tests/coherence/test_coherence_facilitator.py
git commit -m "feat(agents): coherence_facilitator (consensus check, mirrors risk_facilitator)"
```

### Task 5.3: `coherence_arbitrator` agent

**Files:**
- Create: `argosy/agents/coherence_arbitrator.py`
- Test: `tests/coherence/test_coherence_arbitrator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_coherence_arbitrator.py
from argosy.agents.coherence_arbitrator import CoherenceArbitratorAgent, ArbitratorRuling


def test_build_prompt_states_authority_order_and_two_axes():
    agent = CoherenceArbitratorAgent(user_id="ariel")
    system, user = agent.build_prompt(
        dispute_question="which age leads?",
        positions=[{"role": "withdrawal", "position": "46", "basis": "prime_directive"}],
        canonical_facts="earliest_safe_age=46; preservation_age=54",
        prime_directive="maximize finances + earliest safe retirement",
    )
    assert "authority" in system.lower()
    assert "factual" in system.lower() and "policy" in system.lower()
    assert "earliest safe retirement" in user
    assert agent.agent_role == "coherence_arbitrator"


def test_ruling_model_carries_invariant_and_per_surface_instructions():
    r = ArbitratorRuling(
        ruling_statement="age 46 leads; 54 strict track",
        axis="policy", basis="prime_directive", rationale="conservatism costs years",
        per_surface_instructions=[{"surface_id": "long_md", "instruction": "lead with 46"}],
        coherence_invariant=[{"kind": "required_framing_role", "surface": "long_md",
                              "role_field": "lead_age", "value": "46"}],
    )
    assert r.axis == "policy"
    assert r.coherence_invariant[0]["value"] == "46"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_coherence_arbitrator.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `coherence_arbitrator.py`**

```python
# argosy/agents/coherence_arbitrator.py
"""The binding arbitrator for goal/framing tensions. Distinct from fund_manager
(own prompt/schema/telemetry) but embodies its prime-directive authority. Its job
is NOT 'best plan' — it is 'which claim binds under the authority order, and what
invariant must every surface satisfy?'. Two axes: FACTUAL (canonical facts win on
truth) vs POLICY/framing (prime directive > user directives > preference)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent


class ArbitratorRuling(BaseModel):
    ruling_statement: str = Field(description="The binding answer to the dispute.")
    axis: Literal["factual", "policy"] = Field(description="Authority axis used.")
    basis: Literal["prime_directive", "user_directive", "canonical_fact"] = Field(
        description="The binding basis under the axis."
    )
    rationale: str = Field(description="Why this binds, under the authority order.")
    per_surface_instructions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="[{surface_id, instruction}] — how each surface must state the ruling.",
    )
    coherence_invariant: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Typed invariant(s) the verifier will enforce (kind + fields).",
    )


class CoherenceArbitratorAgent(BaseAgent[ArbitratorRuling]):
    agent_role = "coherence_arbitrator"
    output_model = ArbitratorRuling
    require_citations = False
    schema_retry_attempts = 2

    def build_prompt(
        self, *, dispute_question: str, positions: list[dict],
        canonical_facts: str, prime_directive: str,
    ) -> tuple[str, str]:
        system = (
            "You are the Argosy coherence ARBITRATOR. Issue a binding ruling for one "
            "disputed question. First classify the dispute's AUTHORITY AXIS: FACTUAL "
            "(canonical facts win on truth — a directive cannot make a false number "
            "true) vs POLICY/framing (authority order: prime directive > user "
            "directives > panelist preference). Decide WHICH CLAIM BINDS and the exact "
            "INVARIANT every surface must satisfy. You do not design the best plan; you "
            "resolve the contradiction. Emit per-surface instructions and a typed "
            "coherence_invariant the deterministic verifier can check."
        )
        pos = "\n".join(
            f"  - {p.get('role','?')}: {p.get('position','')} (basis={p.get('basis','')})"
            for p in positions
        )
        user = (
            f"PRIME DIRECTIVE:\n{prime_directive}\n\n"
            f"DISPUTED QUESTION:\n{dispute_question}\n\n"
            f"CANONICAL FACTS:\n{canonical_facts}\n\n"
            f"PANELIST POSITIONS:\n{pos}\n"
        )
        return system, user
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_coherence_arbitrator.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/coherence_arbitrator.py tests/coherence/test_coherence_arbitrator.py
git commit -m "feat(agents): coherence_arbitrator (distinct role, two-axis authority, emits invariant)"
```

### Task 5.4: Orchestrating deliberation loop

**Files:**
- Create: `argosy/orchestrator/flows/plan_synthesis/coherence_deliberation.py`
- Test: `tests/coherence/test_deliberation_loop.py`

- [ ] **Step 1: Write the failing test (inject stub agents; no live model)**

```python
# tests/coherence/test_deliberation_loop.py
from argosy.quality.coherence.dispute import Dispute
from argosy.orchestrator.flows.plan_synthesis.coherence_deliberation import (
    deliberate_dispute, DeliberationResult,
)


class _StubFacilitator:
    def __init__(self, consensus, ruling=""): self._c, self._r = consensus, ruling
    def run_sync(self, **kw):
        from types import SimpleNamespace
        from argosy.agents.coherence_facilitator import FacilitatorOutcome
        return SimpleNamespace(output=FacilitatorOutcome(consensus=self._c, ruling=self._r, crux="x"))


class _StubArbitrator:
    def run_sync(self, **kw):
        from types import SimpleNamespace
        from argosy.agents.coherence_arbitrator import ArbitratorRuling
        return SimpleNamespace(output=ArbitratorRuling(
            ruling_statement="age 46 leads; 54 strict track", axis="policy",
            basis="prime_directive", rationale="prime directive",
            per_surface_instructions=[{"surface_id": "long_md", "instruction": "lead 46"}],
            coherence_invariant=[{"kind": "required_framing_role", "surface": "long_md",
                                  "role_field": "lead_age", "value": "46"}]))


def test_no_consensus_escalates_to_arbitrator():
    d = Dispute(subject_type="retirement_age_headline", subject_field_path="",
                scope="person", conflict_type="policy_tension", question="which age leads?")
    res = deliberate_dispute(
        d, panelist_positions=[{"role": "withdrawal", "position": "46", "basis": "prime_directive"}],
        facilitator=_StubFacilitator(consensus=False), arbitrator=_StubArbitrator(),
        canonical_facts="earliest_safe_age=46", prime_directive="earliest safe retirement",
    )
    assert res.resolved_by == "arbitrator"
    assert res.invariant[0]["value"] == "46"


def test_consensus_skips_arbitrator():
    d = Dispute(subject_type="x", subject_field_path="", scope="person",
                conflict_type="policy_tension", question="q")
    res = deliberate_dispute(
        d, panelist_positions=[{"role": "a", "position": "p", "basis": "canonical_fact"}],
        facilitator=_StubFacilitator(consensus=True, ruling="agreed"),
        arbitrator=_StubArbitrator(),
        canonical_facts="", prime_directive="",
    )
    assert res.resolved_by == "consensus"
    assert res.ruling == "agreed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_deliberation_loop.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `coherence_deliberation.py` (the per-dispute step)**

```python
# argosy/orchestrator/flows/plan_synthesis/coherence_deliberation.py
"""Per-dispute deliberation: panel positions -> facilitator -> (arbitrator on
no-consensus). Returns the ruling + typed invariant. The agents are injected so the
loop is unit-testable without a live model. The full-run wiring (cluster -> route ->
resolver/deliberate -> conform -> verify -> ledger -> re-read) is assembled in
Task 5.5 and called from orchestrator.py."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from argosy.quality.coherence.dispute import Dispute


@dataclass
class DeliberationResult:
    resolved_by: str                      # "consensus" | "arbitrator"
    ruling: str
    rationale: str
    basis: str
    invariant: list[dict[str, Any]] = field(default_factory=list)
    per_surface_instructions: list[dict[str, Any]] = field(default_factory=list)


def deliberate_dispute(
    dispute: Dispute, *, panelist_positions: list[dict], facilitator, arbitrator,
    canonical_facts: str, prime_directive: str,
) -> DeliberationResult:
    fac = facilitator.run_sync(
        dispute_question=dispute.question, positions=panelist_positions
    ).output
    if getattr(fac, "consensus", False):
        return DeliberationResult(
            resolved_by="consensus", ruling=fac.ruling, rationale="panel consensus",
            basis="canonical_fact",
        )
    ruling = arbitrator.run_sync(
        dispute_question=dispute.question, positions=panelist_positions,
        canonical_facts=canonical_facts, prime_directive=prime_directive,
    ).output
    return DeliberationResult(
        resolved_by="arbitrator", ruling=ruling.ruling_statement,
        rationale=ruling.rationale, basis=ruling.basis,
        invariant=list(ruling.coherence_invariant),
        per_surface_instructions=list(ruling.per_surface_instructions),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_deliberation_loop.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis/coherence_deliberation.py tests/coherence/test_deliberation_loop.py
git commit -m "feat(synthesis): per-dispute deliberation (panel->facilitator->arbitrator) returning typed invariant"
```

### Task 5.5: Full-run driver + orchestrator wiring

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis/coherence_deliberation.py` (add
  `run_coherence_round`)
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` (call it where the
  blind closer was invoked)
- Test: `tests/coherence/test_coherence_round.py`

- [ ] **Step 1: Write the failing test (deterministic path only; stub the reader)**

```python
# tests/coherence/test_coherence_round.py
from argosy.orchestrator.flows.plan_synthesis.coherence_deliberation import run_coherence_round


def test_value_dispute_conforms_all_surfaces_and_verifies():
    # one value-mismatch dispute: action JSON says SGLN-in-split, body says standalone.
    bodies = {"long_md": "", "medium_md": "SGLN standalone non-UCITS leg",
              "short_md": "SGLN standalone non-UCITS leg"}
    json_surfaces = {"short_actions_json": {"actions": [
        {"label": "First UCITS dollar-cost tranche",
         "detail": "split across CSPX/FUSA/EIMI/SGLN"}]}}

    # a precomputed deterministic patch+invariant for this subject (resolver output)
    resolver_patches = {
        "sgln_ucits_membership": {
            "patches": [{"surface_id": "short_actions_json", "conform_method": "json_field",
                         "match_label": "UCITS dollar-cost", "set_field": "detail",
                         "new_value": "split across CSPX/FUSA/EIMI only; SGLN standalone"}],
            "invariant": [{"kind": "forbidden_claim", "surface": "short_actions_json_text",
                           "pattern": "CSPX/FUSA/EIMI/SGLN"}],
        }
    }
    res = run_coherence_round(
        bodies=bodies, json_surfaces=json_surfaces,
        value_resolutions=resolver_patches, allowed_numbers=frozenset(),
    )
    assert res.ok
    assert "SGLN standalone" in res.json_surfaces["short_actions_json"]["actions"][0]["detail"]
    assert res.verifier.ok
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_coherence_round.py -v`
Expected: FAIL (`run_coherence_round` undefined)

- [ ] **Step 3: Implement `run_coherence_round`**

Append to `coherence_deliberation.py`:

```python
import json as _json

from argosy.quality.coherence.conformer import ConformPatch, apply_patches
from argosy.quality.coherence.invariants import (
    EqualsCanonical, AllRegisteredSurfacesPresent, RequiredFramingRole,
    ForbiddenClaim, SurfaceClaimEquals, verify_invariants, VerifyResult,
)

_INV_CLASSES = {
    "equals_canonical": EqualsCanonical,
    "all_registered_surfaces_present": AllRegisteredSurfacesPresent,
    "required_framing_role": RequiredFramingRole,
    "forbidden_claim": ForbiddenClaim,
    "surface_claim_equals": SurfaceClaimEquals,
}


def _build_invariant(spec: dict):
    kind = spec.get("kind")
    cls = _INV_CLASSES.get(kind)
    if cls is None:
        raise ValueError(f"unknown invariant kind: {kind!r}")
    return cls(**{k: v for k, v in spec.items() if k != "kind"})


@dataclass
class CoherenceRoundResult:
    ok: bool
    bodies: dict
    json_surfaces: dict
    verifier: VerifyResult
    errors: list = field(default_factory=list)


def run_coherence_round(
    *, bodies: dict, json_surfaces: dict,
    value_resolutions: dict[str, dict], allowed_numbers=frozenset(),
) -> CoherenceRoundResult:
    """Apply all resolved patches across surfaces atomically, then verify every
    invariant. Fail-closed: any conform or verify failure => ok=False."""
    patches: list[ConformPatch] = []
    invariants = []
    for _subject, res in value_resolutions.items():
        for p in res.get("patches", []):
            patches.append(ConformPatch(**p))
        for inv in res.get("invariant", []):
            invariants.append(_build_invariant(inv))

    conform = apply_patches(bodies, json_surfaces, patches, allowed_numbers=allowed_numbers)
    if not conform.ok:
        return CoherenceRoundResult(False, bodies, json_surfaces,
                                    VerifyResult(False, conform.errors), conform.errors)

    # build the verifier artifact: markdown surfaces + flattened json text surfaces
    artifact = dict(conform.bodies)
    for sid, payload in conform.json_surfaces.items():
        artifact[f"{sid}_text"] = _json.dumps(payload, ensure_ascii=False)
        artifact[sid] = _json.dumps(payload, ensure_ascii=False)
    verres = verify_invariants(invariants, artifact)
    return CoherenceRoundResult(verres.ok, conform.bodies, conform.json_surfaces, verres)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_coherence_round.py -v`
Expected: PASS

- [ ] **Step 5: Wire into `orchestrator.py`**

Locate where the reader BLOCK currently triggers `surgically_correct_draft` (the
blind closer). Behind a new env flag `ARGOSY_COHERENCE_DELIBERATION=1`, replace that
call with: load active rulings (`ledger.load_active_rulings`), build value
resolutions from the resolver for value-mismatch disputes, run `run_coherence_round`,
persist rulings (`ledger.record_ruling`), and re-invoke `run_whole_artifact_review`
with `settled_rulings=`. On any `ok=False`, BLOCK (do not fall back to the closer).
Keep the old path when the flag is unset.

```python
import os
if os.getenv("ARGOSY_COHERENCE_DELIBERATION") == "1":
    # ... build value_resolutions + deliberate goal tensions, then:
    round_res = run_coherence_round(bodies=bodies, json_surfaces=json_surfaces,
                                    value_resolutions=value_resolutions,
                                    allowed_numbers=allowed)
    if not round_res.ok:
        # fail-closed: persist ledger, BLOCK promotion
        ...
```

- [ ] **Step 6: Run the synthesis-flow regression**

Run: `.venv/Scripts/python.exe -m pytest tests/test_plan_synthesis_surgical_loop.py tests/coherence -m "not llm_eval" -v`
Expected: PASS (existing loop test still green with flag unset; coherence tests green)

- [ ] **Step 7: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis/coherence_deliberation.py argosy/orchestrator/flows/plan_synthesis/orchestrator.py tests/coherence/test_coherence_round.py
git commit -m "feat(synthesis): coherence round driver + orchestrator wiring (flagged, fail-closed)"
```

---

## Slice 6 — Clusterer, value-resolution builder, telemetry appendix, draft-45 e2e

### Task 6.1: Clusterer (reader findings → structured disputes)

**Files:**
- Modify: `argosy/quality/coherence/dispute.py` (add `cluster_findings`)
- Test: `tests/coherence/test_clusterer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_clusterer.py
from argosy.quality.coherence.dispute import cluster_findings, dispute_key


def _f(subject, kind="contradiction", severity="BLOCKER", surfaces=("long_md",)):
    return {"subject_type": subject, "kind": kind, "severity": severity,
            "field_path": "", "normalized_claim": "", "surfaces_cited": list(surfaces),
            "detail": "d"}


def test_findings_with_same_subject_cluster_to_one_dispute():
    disputes = cluster_findings([_f("rsu_vest_policy", surfaces=("long_md",)),
                                 _f("rsu_vest_policy", surfaces=("short_actions_json",))])
    assert len(disputes) == 1
    assert disputes[0].subject_type == "rsu_vest_policy"
    assert set(disputes[0].surfaces_cited) == {"long_md", "short_actions_json"}


def test_policy_tension_kind_maps_conflict_type():
    disputes = cluster_findings([_f("retirement_age_headline", kind="fragile_claim")])
    assert disputes[0].conflict_type == "policy_tension"


def test_untyped_finding_yields_block_dispute():
    disputes = cluster_findings([_f("", kind="contradiction")])
    assert disputes[0].subject_type == ""  # router will BLOCK this
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_clusterer.py -v`
Expected: FAIL (`cluster_findings` undefined)

- [ ] **Step 3: Add `cluster_findings` to `dispute.py`**

```python
# append to argosy/quality/coherence/dispute.py

# reader finding kind -> dispute conflict_type
_KIND_TO_CONFLICT = {
    "contradiction": "value_mismatch",
    "cross_surface": "value_mismatch",
    "calc_inconsistency": "calc_inconsistency",
    "stale": "value_mismatch",
    "regression": "value_mismatch",
    "fragile_claim": "policy_tension",   # goal/framing tension
    "other": "policy_tension",
}


def cluster_findings(findings: list[dict]) -> list[Dispute]:
    """Group typed reader findings into one Dispute per (subject_type, conflict_type).
    Surface ids accumulate as evidence. A finding with no subject_type becomes an
    untyped dispute (the router will BLOCK it)."""
    grouped: dict[tuple[str, str], dict] = {}
    for f in findings:
        subject = (f.get("subject_type") or "").strip()
        conflict = _KIND_TO_CONFLICT.get(f.get("kind", "other"), "policy_tension")
        gk = (subject, conflict)
        g = grouped.setdefault(gk, {"surfaces": set(), "fields": set(), "options": set(),
                                    "questions": []})
        g["surfaces"].update(f.get("surfaces_cited") or [])
        if f.get("field_path"):
            g["fields"].add(f["field_path"])
        if f.get("normalized_claim"):
            g["options"].add(f["normalized_claim"])
        g["questions"].append(f.get("detail") or "")
    out: list[Dispute] = []
    for (subject, conflict), g in grouped.items():
        out.append(Dispute(
            subject_type=subject,
            subject_field_path=sorted(g["fields"])[0] if g["fields"] else "",
            scope="person", conflict_type=conflict,
            normalized_options=tuple(sorted(g["options"])),
            question=g["questions"][0] if g["questions"] else "",
            surfaces_cited=tuple(sorted(g["surfaces"])),
        ))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_clusterer.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/coherence/dispute.py tests/coherence/test_clusterer.py
git commit -m "feat(coherence): clusterer (typed reader findings -> structured disputes)"
```

### Task 6.2: Value-resolution builder (canonical facts + registry → patches + invariant)

**Files:**
- Modify: `argosy/quality/coherence/resolver_route.py` (add `build_value_resolution`)
- Test: `tests/coherence/test_value_resolution.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_value_resolution.py
from argosy.quality.coherence.dispute import Dispute
from argosy.quality.coherence.resolver_route import build_value_resolution


def test_builds_equals_canonical_patches_for_markdown_sites():
    d = Dispute(subject_type="nvda_cap", subject_field_path="concentration.nvda_cap_pct",
                scope="person", conflict_type="value_mismatch", question="q",
                surfaces_cited=("long_md", "short_md"))
    # registry-derived sites + the canonical text the surfaces must state
    sites = [("long_md", "markdown"), ("short_md", "markdown")]
    res = build_value_resolution(d, canonical_text="13.0", sites=sites,
                                 stale_text="12.0")
    # one patch per site replacing the stale text, plus an equals_canonical invariant
    assert all(p["conform_method"] == "markdown" for p in res["patches"])
    assert {p["surface_id"] for p in res["patches"]} == {"long_md", "short_md"}
    assert any(i["kind"] == "equals_canonical" for i in res["invariant"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_value_resolution.py -v`
Expected: FAIL (`build_value_resolution` undefined)

- [ ] **Step 3: Add `build_value_resolution` to `resolver_route.py`**

```python
# append to argosy/quality/coherence/resolver_route.py

def build_value_resolution(
    dispute: "Dispute", *, canonical_text: str, sites: list[tuple[str, str]],
    stale_text: str = "",
) -> dict:
    """Build a deterministic patch+invariant set conforming every site to the
    canonical value. `sites` is [(surface_id, conform_method)]. For markdown sites a
    stale_text->canonical_text replacement is emitted (no-op if stale_text empty).
    The invariant asserts equals_canonical across all sites."""
    patches: list[dict] = []
    for surface_id, method in sites:
        if method == "markdown" and stale_text:
            patches.append({
                "surface_id": surface_id, "conform_method": "markdown",
                "find": stale_text, "replace": canonical_text,
            })
        # json_field patches are subject-specific and supplied by the caller's
        # registry mapping; markdown value swaps are the generic case here.
    invariant = [{
        "kind": "equals_canonical",
        "subject_type": dispute.subject_type,
        "canonical_text": canonical_text,
        "surfaces": tuple(s for s, _ in sites),
    }]
    return {"patches": patches, "invariant": invariant}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_value_resolution.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/coherence/resolver_route.py tests/coherence/test_value_resolution.py
git commit -m "feat(coherence): value-resolution builder (canonical text -> all-surface patches + equals_canonical invariant)"
```

### Task 6.3: Deliberation telemetry appendix

**Files:**
- Modify: `argosy/services/plan_export.py` (render the appendix from the ledger)
- Modify: `argosy/services/assembled_artifact.py` (add the heading to
  `_INTERNAL_METADATA_HEADINGS` so it is stripped from the reader artifact)
- Test: `tests/coherence/test_deliberation_appendix.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_deliberation_appendix.py
from argosy.services.plan_export import render_coherence_deliberation_appendix
from argosy.services.assembled_artifact import _strip_internal_metadata_sections


def test_appendix_renders_one_row_per_ruling():
    rows = [{"subject_type": "retirement_age_headline", "question": "which age leads?",
             "resolved_by": "arbitrator", "ruling": "age 46 leads; 54 strict track",
             "conformed_surfaces": ["long_md", "medium_md"]}]
    md = render_coherence_deliberation_appendix(rows)
    assert "## Appendix — Coherence deliberations" in md
    assert "retirement_age_headline" in md
    assert "arbitrator" in md


def test_appendix_is_stripped_from_reader_artifact():
    art = "## Current Plan\nbody\n\n## Appendix — Coherence deliberations\nrow\n"
    stripped = _strip_internal_metadata_sections(art)
    assert "Coherence deliberations" not in stripped
    assert "## Current Plan" in stripped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_deliberation_appendix.py -v`
Expected: FAIL (`render_coherence_deliberation_appendix` undefined)

- [ ] **Step 3: Implement the appendix + register the heading for stripping**

In `argosy/services/plan_export.py`:

```python
def render_coherence_deliberation_appendix(rows: list[dict]) -> str:
    """One row per coherence ruling: question -> resolution -> ruling -> surfaces.
    Internal metadata: ships in the user export, stripped from the reader artifact."""
    if not rows:
        return ""
    lines = ["## Appendix — Coherence deliberations", "",
             "| Subject | Question | Resolved by | Ruling | Surfaces conformed |",
             "|---|---|---|---|---|"]
    for r in rows:
        surfaces = ", ".join(r.get("conformed_surfaces") or [])
        q = (r.get("question") or "").replace("|", "\\|")[:80]
        ruling = (r.get("ruling") or "").replace("|", "\\|")[:120]
        lines.append(
            f"| {r.get('subject_type','')} | {q} | {r.get('resolved_by','')} | {ruling} | {surfaces} |"
        )
    return "\n".join(lines)
```

In `argosy/services/assembled_artifact.py`, add the heading to the strip tuple:

```python
_INTERNAL_METADATA_HEADINGS = (
    "## Appendix — Fleet receipts",
    "## Appendix — Analysis team receipts",
    "## Appendix — Coherence deliberations",
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_deliberation_appendix.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/services/plan_export.py argosy/services/assembled_artifact.py tests/coherence/test_deliberation_appendix.py
git commit -m "feat(telemetry): coherence-deliberations appendix (export-visible, reader-stripped)"
```

### Task 6.4: Draft-45 end-to-end acceptance (mocked agents, real conform+verify)

**Files:**
- Test: `tests/coherence/test_draft45_e2e.py`

This test proves the mechanism closes draft 45's four disputes deterministically end
to end: vest policy + SGLN + date (value/representation → resolver+conformer) and the
retirement-age framing tension (policy → arbitration → framing contract). Agents are
stubbed; conform + verify are real.

- [ ] **Step 1: Write the failing test**

```python
# tests/coherence/test_draft45_e2e.py
from argosy.orchestrator.flows.plan_synthesis.coherence_deliberation import run_coherence_round
from argosy.quality.coherence.claim_markers import render_marker


def test_draft45_disputes_conform_and_verify():
    age_marker = render_marker("retirement_age_headline",
                               {"lead_age": "46", "strict_track_age": "54",
                                "capital_preservation_role": "target_sizing_basis"})
    bodies = {
        "long_md": f"Retirement framing. {age_marker}",
        "medium_md": "SGLN standalone non-UCITS leg",
        "short_md": "sell net vested NVDA -> SGOV",
    }
    json_surfaces = {"short_actions_json": {"actions": [
        {"label": "First UCITS dollar-cost tranche", "detail": "split across CSPX/FUSA/EIMI/SGLN"},
        {"label": "Sell 2026-06-17 net vested NVDA", "detail": "route net-of-tax to SGOV"},
    ]}}

    value_resolutions = {
        "sgln_ucits_membership": {
            "patches": [{"surface_id": "short_actions_json", "conform_method": "json_field",
                         "match_label": "UCITS dollar-cost", "set_field": "detail",
                         "new_value": "split across CSPX/FUSA/EIMI only; SGLN standalone"}],
            "invariant": [{"kind": "forbidden_claim", "surface": "short_actions_json",
                           "pattern": "CSPX/FUSA/EIMI/SGLN"}],
        },
        "retirement_age_headline": {
            "patches": [],  # framing already carried by the typed marker
            "invariant": [
                {"kind": "required_framing_role", "subject_type": "retirement_age_headline",
                 "surface": "long_md", "role_field": "lead_age", "value": "46"},
                {"kind": "required_framing_role", "subject_type": "retirement_age_headline",
                 "surface": "long_md", "role_field": "capital_preservation_role",
                 "value": "target_sizing_basis"},
                {"kind": "forbidden_claim", "subject_type": "rsu_vest_policy",
                 "surface": "short_md", "pattern": "retain net vested as NVDA"},
            ],
        },
    }

    res = run_coherence_round(bodies=bodies, json_surfaces=json_surfaces,
                              value_resolutions=value_resolutions, allowed_numbers=frozenset())
    assert res.ok, res.verifier.failures
    assert "SGLN standalone" in res.json_surfaces["short_actions_json"]["actions"][0]["detail"]
    assert res.verifier.ok
```

- [ ] **Step 2: Run test to verify it fails, then passes**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence/test_draft45_e2e.py -v`
Expected: PASS (the mechanism from Slices 1–5 already supports this; if it fails,
the failure message names the unmet invariant — fix the implicated slice, do not
weaken the test).

- [ ] **Step 3: Run the full coherence suite + synthesis regression**

Run: `.venv/Scripts/python.exe -m pytest tests/coherence tests/test_plan_synthesis_surgical_loop.py tests/test_plan_output_gate.py tests/test_surgical_reconcile.py tests/test_run106_coverage.py -m "not llm_eval" -v`
Expected: PASS (all green)

- [ ] **Step 4: Commit**

```bash
git add tests/coherence/test_draft45_e2e.py
git commit -m "test(coherence): draft-45 e2e — four disputes conform + verify end to end"
```

### Task 6.5: Live draft-45 promotion (the acceptance milestone)

**Files:**
- Create (gitignored): `tmp_review/promote_draft45_via_deliberation.py`

This is the live, full-mechanism run that promotes draft 45. It runs against
`db/argosy.db` with `ARGOSY_COHERENCE_DELIBERATION=1`, executes a real reader →
deliberation → conform → verify → re-read cycle, and promotes only on the explicit
promotion gate (Slice spec): verifier green, re-read emits no
`new_dispute`/`ruling_divergence`/`ruling_defect`, focused regression green.

- [ ] **Step 1: Write the driver**

Mirror `tmp_review/coherence_zigzag.py`'s structure but call the new
`run_coherence_round` + `run_whole_artifact_review(settled_rulings=...)`. Promote
(set `role='current'`, supersede prior) only when the gate passes; write
`tmp_review/overnight.DONE`. NEVER promote on a BLOCK. Use UTF-8 for the report.

- [ ] **Step 2: Run it (background) and confirm promotion**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tmp_review/promote_draft45_via_deliberation.py`
Expected: report shows verifier green + reader no-appeal + `*** PROMOTED draft 45 ***`.
Verify: the latest `plan_versions` row for ariel has `role='current'`.

- [ ] **Step 3: Update auto-memory + SDD handover**

Update `project_coherence_reconcile_loop_and_gates` memory with the shipped mechanism
+ draft-45 promotion; refresh the SDD handover note.

---

## Self-Review

**Spec coverage** (each spec section → task):
- Resolver/panel split → Task 1.5 (router) + 6.2 (value res) + 5.4 (deliberation).
- Structured dispute identity → Task 1.1; clusterer → 6.1.
- Surface registry + derived deps → Task 1.2 (derived deps field present; populated in 6).
- Invariant DSL + verifier → 1.3 (value) + 2.2 (framing); verifier gate → 5.5.
- Framing contract + typed claim markers → 2.1 + 2.2.
- Conformer (atomic, idempotent, number guard, all surfaces) → 1.4.
- Distinct coherence_arbitrator + two axes → 5.3.
- Panel + facilitator → 5.1 + 5.2; per-dispute loop → 5.4; full run → 5.5.
- Ledger + supersession → 3.1 + 3.2 + 3.3.
- Reader appeal (no laundering) → 4.1 + 4.2.
- Fail-closed = BLOCK → 1.4 (conform), 5.5 (round), orchestrator wiring.
- Telemetry appendix (export-visible, reader-stripped) → 6.3.
- Draft-45 acceptance → 6.4 (e2e) + 6.5 (live promotion).

**Type consistency:** `Dispute`, `dispute_key`, `SurfaceSite`, `ConformPatch`,
`apply_patches`, `verify_invariants`, `RouteKind`, `DeliberationResult`,
`CoherenceRoundResult`, `CoherenceDecision`, `ArbitratorRuling`, `FacilitatorOutcome`,
`PanelistPosition` are referenced with consistent signatures across tasks. Invariant
kind strings (`equals_canonical`, `all_registered_surfaces_present`,
`required_framing_role`, `forbidden_claim`, `surface_claim_equals`) match between
`invariants.py`, `_INV_CLASSES`, and the ruling/value-resolution specs.

**Known follow-ups (not blocking this plan):** derived-field recompute for dashboard
surfaces (registry `derived_from` is declared but dashboard recompute is wired in
Slice 6 only for the draft-45 subjects); the live resolver that maps each value
dispute to its canonical text + registry sites is exercised generically in 6.2 and
specifically in the 6.5 driver.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-17-coherence-deliberation.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
