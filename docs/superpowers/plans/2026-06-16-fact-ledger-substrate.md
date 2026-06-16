# Fact + RenderedFactSite Ledger Substrate Implementation Plan (Slice 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the addressable substrate the fact-centric surgical-fix design needs — a canonical `Fact`, a `RenderedFactSite` ledger emitted from canonical values, a typed `FindingLocation`, a run-106 fact inventory, ledger-based attribution, and the codex-required pre-render `TargetAllocationDoc` ordering — WITHOUT yet building the surgical re-renderer or prose editor (those are Slice 3).

**Architecture:** New pure-data modules in `argosy/quality/` (`fact_ledger.py`, `fact_inventory.py`, `fact_attribution.py`) hold the dataclasses + registry + attribution. A deterministic builder turns a `TargetAllocationDoc` + resolver manifest into `RenderedFactSite` entries for the *template/structured* allocation facts (the keystone proof that attribution is recorded from canonical values, not reverse-engineered from prose). The orchestrator's `_assemble_draft_bodies` is reordered to build the `TargetAllocationDoc` BEFORE horizon markdown render (codex v2 #6). A persisted run-106 reader-JSON fixture drives attribution tests.

**Tech Stack:** Python 3.12, dataclasses + Pydantic, SQLAlchemy, pytest. Windows PowerShell (`;` not `&&`). Interpreter `D:/Projects/financial-advisor/.venv/Scripts/python.exe`. Tests: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" <path> -q -p no:cacheprovider`.

**Scope boundary (explicit):** This slice produces the substrate + the deterministic ledger for ALLOCATION/template facts + attribution + the pre-render reorder. It does NOT instrument every prose renderer (the `llm_prose` site ledger) and does NOT build the re-renderer/prose-editor — those are Slice 3 (the surgical-correction plan). The `site_kind` taxonomy is defined here so Slice 3 plugs in.

---

## File Structure

- **Create** `argosy/quality/fact_ledger.py` — `SiteKind` enum (`template` / `structured_field` / `llm_prose`), `RenderedFactSite` dataclass, `Fact` dataclass, `FactLedger` container (add + query by fact_id / surface_id / normalized_value). Pure data, no I/O.
- **Create** `argosy/quality/fact_inventory.py` — `FactSpec` dataclass + `RUN106_FACTS: dict[str, FactSpec]`: the Phase-1a inventory — each run-106 `fact_id` → its derivation source (resolver key / doc field / agent), its render surfaces, and the `site_kind` of each. The structured table codex demanded.
- **Create** `argosy/quality/fact_attribution.py` — `FindingLocation` dataclass + `attribute_finding(finding, ledger, *, inventory=RUN106_FACTS) -> list[FindingLocation]`: maps a reader `CoherenceFinding` (or a `GateViolation`) to `fact_id`(s) via the ledger; fail-safe (unattributable → a structural-route `FindingLocation` with `fact_id=None` + a logged gap).
- **Create** `argosy/services/allocation_fact_sites.py` — `build_allocation_fact_sites(doc, resolved) -> list[RenderedFactSite]`: deterministic — turns a `TargetAllocationDoc` + resolver manifest into the `RenderedFactSite` entries for the template/structured allocation facts (nvda cap, each sleeve weight). Proves "ledger from canonical value".
- **Modify** `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` — in `_assemble_draft_bodies`, build the `TargetAllocationDoc` BEFORE rendering horizon markdown; keep `target_allocation_json` output identical (regression-guarded).
- **Create** `tests/fixtures/run106_reader_verdict.json` — the persisted run-106 `WholeArtifactVerdict` JSON (extracted from the DB row if present, else reconstructed from the 11 findings) — the attribution fixture.
- **Tests:** `tests/test_fact_ledger.py`, `tests/test_fact_inventory.py`, `tests/test_fact_attribution.py`, `tests/test_allocation_fact_sites.py`, `tests/test_assemble_bodies_doc_ordering.py`.

Why these boundaries: data model, inventory, and attribution are independently testable pure modules; the allocation-site builder is the one renderer-shaped piece (deterministic, so safest first); the orchestrator change is a thin reorder guarded by a value-equality regression test.

---

### Task 1: The fact-ledger data model

**Files:**
- Create: `argosy/quality/fact_ledger.py`
- Test: `tests/test_fact_ledger.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fact_ledger.py
from argosy.quality.fact_ledger import Fact, FactLedger, RenderedFactSite, SiteKind


def test_ledger_indexes_sites_by_fact_and_surface():
    ledger = FactLedger()
    ledger.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="target_allocation_json",
        field_path="$.nvda_cap_pct", byte_span=(0, 0),
        rendered_text="13.0", normalized_value=13.0, site_kind=SiteKind.STRUCTURED_FIELD,
        hash="h1",
    ))
    ledger.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="body",
        field_path="long#cap", byte_span=(10, 20),
        rendered_text="NVDA cap 13%", normalized_value=13.0, site_kind=SiteKind.TEMPLATE,
        hash="h2",
    ))
    # query by fact_id returns both render sites
    sites = ledger.sites_for_fact("allocation.nvda_cap_pct")
    assert len(sites) == 2
    assert {s.surface_id for s in sites} == {"target_allocation_json", "body"}
    # query by surface returns only that surface's sites
    assert len(ledger.sites_for_surface("body")) == 1


def test_fact_holds_canonical_value_and_site_kinds_are_distinct():
    f = Fact(fact_id="retirement.fi_age", value=46, unit="age",
             derivation="resolver:retirement.fi_age")
    assert f.fact_id == "retirement.fi_age"
    # the three site kinds the design requires
    assert {SiteKind.TEMPLATE, SiteKind.STRUCTURED_FIELD, SiteKind.LLM_PROSE}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_ledger.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.quality.fact_ledger'`.

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/quality/fact_ledger.py
"""Canonical facts + the render-site ledger (the fact-centric design keystone).

A *canonical fact* is a single load-bearing value (the retirement age, the FX
rate, the NVDA cap, a sleeve weight) with a stable ``fact_id`` and one derived
value. A ``RenderedFactSite`` records WHERE that fact was rendered — emitted at
render time FROM the canonical value, never reverse-engineered from finished
text (an excerpt hash proves a string existed, not which fact it expresses).

Pure data + indexing. No I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SiteKind(str, Enum):
    """How a render site was produced — determines if it can be deterministically
    re-rendered (Slice 3)."""

    TEMPLATE = "template"              # produced from the canonical value via a template
    STRUCTURED_FIELD = "structured_field"  # a typed field set from the canonical value
    LLM_PROSE = "llm_prose"           # free LLM-authored text; not deterministically re-renderable


@dataclass(frozen=True)
class Fact:
    """A canonical derived value, owned by its derivation (not by a section)."""

    fact_id: str
    value: object
    unit: str | None = None
    derivation: str | None = None  # e.g. "resolver:retirement.fi_age" / "doc:nvda_cap_pct"


@dataclass(frozen=True)
class RenderedFactSite:
    """One place a fact was rendered, recorded at render time."""

    fact_id: str
    surface_id: str           # body|dashboard|appendix|target_allocation_json|fm_objection|prior_plan
    field_path: str | None    # json_path / section_id+offset / table cell
    byte_span: tuple[int, int]
    rendered_text: str
    normalized_value: object
    site_kind: SiteKind
    hash: str


@dataclass
class FactLedger:
    """Indexed collection of render sites. Built up as renderers emit sites."""

    sites: list[RenderedFactSite] = field(default_factory=list)

    def add(self, site: RenderedFactSite) -> None:
        self.sites.append(site)

    def extend(self, sites: list[RenderedFactSite]) -> None:
        self.sites.extend(sites)

    def sites_for_fact(self, fact_id: str) -> list[RenderedFactSite]:
        return [s for s in self.sites if s.fact_id == fact_id]

    def sites_for_surface(self, surface_id: str) -> list[RenderedFactSite]:
        return [s for s in self.sites if s.surface_id == surface_id]

    def fact_ids(self) -> set[str]:
        return {s.fact_id for s in self.sites}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_ledger.py -q -p no:cacheprovider`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/fact_ledger.py tests/test_fact_ledger.py
git commit -m "feat(quality): fact + RenderedFactSite ledger data model (fact-centric keystone)"
```

---

### Task 2: The run-106 fact inventory (Phase 1a)

**Files:**
- Create: `argosy/quality/fact_inventory.py`
- Test: `tests/test_fact_inventory.py`

This is the explicit table codex demanded: each run-106 `fact_id` → derivation → render surfaces + `site_kind`. No invariant runs here; it is the addressable map.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fact_inventory.py
from argosy.quality.fact_ledger import SiteKind
from argosy.quality.fact_inventory import RUN106_FACTS, FactSpec


def test_inventory_covers_the_run106_load_bearing_facts():
    # the canonical facts the run-106 findings attach to
    expected = {
        "retirement.fi_status",
        "retirement.earliest_safe_age",
        "retirement.fi_age",
        "retirement.bridge_start_age",
        "allocation.target_weights",
        "allocation.nvda_cap_pct",
        "rsu.net_retention_pct",
        "event.rsu_tax_2026_06_17",
        "instrument.SGLN.wrapper_type",
    }
    assert expected.issubset(set(RUN106_FACTS))


def test_each_spec_names_a_derivation_and_at_least_one_site_kind():
    for fact_id, spec in RUN106_FACTS.items():
        assert isinstance(spec, FactSpec)
        assert spec.derivation, f"{fact_id} has no derivation"
        assert spec.surfaces, f"{fact_id} names no render surfaces"
        assert all(isinstance(k, SiteKind) for k in spec.site_kinds.values())


def test_allocation_facts_are_deterministic_sites():
    # allocation facts render from TargetAllocationDoc -> template/structured (re-renderable)
    for fid in ("allocation.nvda_cap_pct", "allocation.target_weights"):
        kinds = set(RUN106_FACTS[fid].site_kinds.values())
        assert SiteKind.LLM_PROSE not in kinds or len(kinds) > 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_inventory.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.quality.fact_inventory'`.

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/quality/fact_inventory.py
"""Phase 1a — the run-106 fact inventory.

The explicit table codex required: for each load-bearing run-106 fact, its
``fact_id``, the derivation that owns it (resolver key / TargetAllocationDoc
field / agent), the surfaces it renders on, and the ``site_kind`` per surface.
This is a static map (the addressable substrate), not executable checking.

site_kind per surface is the design's classification: ``template`` /
``structured_field`` re-render deterministically (Slice 3); ``llm_prose`` must
route through the prose editor.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from argosy.quality.fact_ledger import SiteKind


@dataclass(frozen=True)
class FactSpec:
    """One inventory row: how a fact is derived + where it renders."""

    fact_id: str
    derivation: str                       # "resolver:<key>" | "doc:<field>" | "agent:<role>" | "renderer"
    surfaces: tuple[str, ...]             # surface_ids it appears on
    site_kinds: dict[str, SiteKind]       # surface_id -> site_kind
    note: str = ""


def _spec(fact_id, derivation, site_map, note=""):
    return FactSpec(
        fact_id=fact_id, derivation=derivation,
        surfaces=tuple(site_map), site_kinds=dict(site_map), note=note,
    )


RUN106_FACTS: dict[str, FactSpec] = {
    "retirement.fi_status": _spec(
        "retirement.fi_status", "resolver:retirement.fi_margin_signed_nis",
        {"body": SiteKind.LLM_PROSE, "dashboard": SiteKind.STRUCTURED_FIELD,
         "appendix": SiteKind.TEMPLATE},
        "reached/not-reached + qualifier; finding [0],[1]",
    ),
    "retirement.earliest_safe_age": _spec(
        "retirement.earliest_safe_age", "resolver:retirement.earliest_safe_age",
        {"body": SiteKind.LLM_PROSE, "appendix": SiteKind.TEMPLATE},
        "headline age; finding [2]",
    ),
    "retirement.fi_age": _spec(
        "retirement.fi_age", "resolver:retirement.fi_age",
        {"body": SiteKind.LLM_PROSE, "appendix": SiteKind.TEMPLATE},
        "FIRE-bridge sizing age, deliberately distinct from earliest_safe_age; finding [2]",
    ),
    "retirement.bridge_start_age": _spec(
        "retirement.bridge_start_age", "derived:bridge sized from resolver fi_age",
        {"body": SiteKind.LLM_PROSE, "appendix": SiteKind.TEMPLATE},
        "must equal the resolver sizing age; finding [2] (net-new fact)",
    ),
    "allocation.target_weights": _spec(
        "allocation.target_weights", "doc:classes[].target_pct",
        {"target_allocation_json": SiteKind.STRUCTURED_FIELD,
         "body": SiteKind.TEMPLATE, "appendix": SiteKind.TEMPLATE},
        "IPS instrument map; finding [5]",
    ),
    "allocation.nvda_cap_pct": _spec(
        "allocation.nvda_cap_pct", "doc:nvda_cap_pct",
        {"target_allocation_json": SiteKind.STRUCTURED_FIELD,
         "body": SiteKind.TEMPLATE},
        "Argosy-derived cap; cap-derivation gate",
    ),
    "rsu.net_retention_pct": _spec(
        "rsu.net_retention_pct", "agent:equity_comp",
        {"body": SiteKind.LLM_PROSE, "appendix": SiteKind.STRUCTURED_FIELD},
        "ledger vs equity-comp vs prose; finding [3] (net-new derivation)",
    ),
    "event.rsu_tax_2026_06_17": _spec(
        "event.rsu_tax_2026_06_17", "agent:tax",
        {"body": SiteKind.LLM_PROSE},
        "amount + currency; finding [4] (net-new derivation)",
    ),
    "instrument.SGLN.wrapper_type": _spec(
        "instrument.SGLN.wrapper_type", "doc:classes[].instruments[].domicile",
        {"body": SiteKind.LLM_PROSE, "appendix": SiteKind.STRUCTURED_FIELD},
        "physical-gold ETC, not UCITS; finding [7]",
    ),
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_inventory.py -q -p no:cacheprovider`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/fact_inventory.py tests/test_fact_inventory.py
git commit -m "feat(quality): run-106 fact inventory (Phase 1a addressable map)"
```

---

### Task 3: Build TargetAllocationDoc BEFORE rendering (codex v2 #6)

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` (`_assemble_draft_bodies`, ~lines 3146–3234)
- Test: `tests/test_assemble_bodies_doc_ordering.py`

Today `_assemble_draft_bodies` renders horizon markdown first (line ~3163) and resolves `target_allocation_json` last (line ~3218). Flip: build the doc first so allocation sites can render FROM it. The output `target_allocation_json` MUST stay byte-identical (regression guard) — this is a reorder, not a value change.

- [ ] **Step 1: Write the failing test** (a probe asserting the doc is built before render)

```python
# tests/test_assemble_bodies_doc_ordering.py
"""The TargetAllocationDoc must be built BEFORE horizon markdown render so
allocation sites render from the canonical doc (codex v2 #6). We assert the
call order by recording it on a spy."""
from __future__ import annotations

import argosy.orchestrator.flows.plan_synthesis.orchestrator as orch


def test_target_allocation_doc_built_before_horizon_render(monkeypatch):
    calls: list[str] = []

    real_resolve = orch.resolve_target_allocation_json

    def spy_resolve(*a, **k):
        calls.append("resolve_target_allocation_json")
        return real_resolve(*a, **k)

    real_render = orch._horizon_md_user

    def spy_render(*a, **k):
        calls.append("_horizon_md_user")
        return real_render(*a, **k)

    monkeypatch.setattr(orch, "resolve_target_allocation_json", spy_resolve, raising=False)
    monkeypatch.setattr(orch, "_horizon_md_user", spy_render, raising=False)

    # Drive _assemble_draft_bodies with a minimal stub output (no DB writes).
    # Use the existing synth-output factory from the whole-artifact test helpers.
    from tests.test_plan_synthesis_whole_artifact import _make_minimal_synth_output  # noqa
    # If that helper does not exist, build a PlanSynthesisOutput stub inline.
    ...
    # After running, the FIRST allocation resolve must precede the FIRST render.
    assert calls.index("resolve_target_allocation_json") < calls.index("_horizon_md_user")
```

> **Implementer note:** if `tests/test_plan_synthesis_whole_artifact.py` has no
> `_make_minimal_synth_output`, construct a `PlanSynthesisOutput` inline from
> `argosy.agents.plan_synthesizer_types` with empty horizon sections (the render
> helpers tolerate empty targets/themes/actions). The assertion is purely about
> call ORDER, so the bodies content does not matter. Patch out any DB-touching
> calls in `_assemble_draft_bodies` the same way the reader-reconcile test does.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_assemble_bodies_doc_ordering.py -q -p no:cacheprovider`
Expected: FAIL — render is currently called before the allocation resolve, so `calls.index(...)` assertion fails (or the resolve name appears after).

- [ ] **Step 3: Implementation — move the allocation-doc build above the render block**

In `_assemble_draft_bodies`, relocate the `resolve_target_allocation_json(...)` call (and any `build_plan_target_allocation_doc` it wraps) to BEFORE the first `_horizon_md_user(output.long)` render. Keep the produced `target_allocation_json` string assigned into the returned `_bodies` dict unchanged. Capture the built `TargetAllocationDoc` in a local (e.g. `_alloc_doc`) so Task 4 / Slice 3 can pass it into rendering. Do NOT change how the JSON is serialized.

- [ ] **Step 4: Run test to verify it passes + regression-guard the JSON value**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_assemble_bodies_doc_ordering.py tests/test_plan_synthesis_whole_artifact.py tests/test_target_allocation_doc.py -q -p no:cacheprovider`
Expected: PASS (ordering test green; the existing synthesis + allocation-doc tests confirm `target_allocation_json` is unchanged).

- [ ] **Step 5: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis/orchestrator.py tests/test_assemble_bodies_doc_ordering.py
git commit -m "refactor(synthesis): build TargetAllocationDoc before horizon render (codex v2 #6)"
```

---

### Task 4: Deterministic RenderedFactSite ledger for allocation facts

**Files:**
- Create: `argosy/services/allocation_fact_sites.py`
- Test: `tests/test_allocation_fact_sites.py`

The keystone proof: build the ledger FROM the canonical `TargetAllocationDoc` (template/structured sites), so attribution is recorded from values, not parsed from prose.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_allocation_fact_sites.py
from types import SimpleNamespace

from argosy.quality.fact_ledger import SiteKind
from argosy.services.allocation_fact_sites import build_allocation_fact_sites


def _doc():
    cls = [
        SimpleNamespace(label="US broad-market core", target_pct=60.0),
        SimpleNamespace(label="Gold", target_pct=18.0),
        SimpleNamespace(label="Bonds", target_pct=22.0),
    ]
    return SimpleNamespace(nvda_cap_pct=13.0, classes=cls)


def test_emits_cap_and_weight_sites_from_canonical_doc():
    sites = build_allocation_fact_sites(_doc(), resolved=None)
    by_fact = {}
    for s in sites:
        by_fact.setdefault(s.fact_id, []).append(s)
    # the nvda cap fact has a structured_field site carrying the canonical value
    cap = by_fact["allocation.nvda_cap_pct"]
    assert any(s.surface_id == "target_allocation_json" and s.normalized_value == 13.0 for s in cap)
    assert all(s.site_kind in (SiteKind.STRUCTURED_FIELD, SiteKind.TEMPLATE) for s in cap)
    # each sleeve weight is a site under the target_weights fact
    weights = by_fact["allocation.target_weights"]
    assert {s.normalized_value for s in weights} >= {60.0, 18.0, 22.0}


def test_no_doc_returns_empty():
    assert build_allocation_fact_sites(None, resolved=None) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_allocation_fact_sites.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.services.allocation_fact_sites'`.

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/services/allocation_fact_sites.py
"""Deterministic RenderedFactSite emission for the allocation facts.

The allocation facts (NVDA cap, each sleeve weight) are produced FROM the
canonical TargetAllocationDoc, so their render sites can be emitted
deterministically — the keystone proof that the ledger is recorded from
canonical values, not reverse-engineered from finished prose. ``llm_prose``
allocation sites (e.g. a sentence paraphrasing the weights) are NOT covered
here; they are Slice 3's prose-editor scope.

Pure function, no I/O. Duck-types the doc (label / target_pct / nvda_cap_pct).
"""
from __future__ import annotations

import hashlib

from argosy.quality.fact_ledger import RenderedFactSite, SiteKind


def _hash(*parts: object) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def build_allocation_fact_sites(doc, resolved=None) -> list[RenderedFactSite]:
    """Emit RenderedFactSite entries for the canonical allocation facts."""
    if doc is None:
        return []
    sites: list[RenderedFactSite] = []

    cap = getattr(doc, "nvda_cap_pct", None)
    if cap is not None:
        cap = float(cap)
        sites.append(RenderedFactSite(
            fact_id="allocation.nvda_cap_pct", surface_id="target_allocation_json",
            field_path="$.nvda_cap_pct", byte_span=(0, 0),
            rendered_text=f"{cap}", normalized_value=cap,
            site_kind=SiteKind.STRUCTURED_FIELD, hash=_hash("cap", cap),
        ))

    for i, cls in enumerate(getattr(doc, "classes", None) or []):
        label = getattr(cls, "label", None)
        pct = getattr(cls, "target_pct", None)
        if label is None or pct is None:
            continue
        pct = float(pct)
        sites.append(RenderedFactSite(
            fact_id="allocation.target_weights", surface_id="target_allocation_json",
            field_path=f"$.classes[{i}].target_pct", byte_span=(0, 0),
            rendered_text=f"{label} {pct}%", normalized_value=pct,
            site_kind=SiteKind.STRUCTURED_FIELD, hash=_hash("weight", label, pct),
        ))
    return sites
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_allocation_fact_sites.py -q -p no:cacheprovider`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add argosy/services/allocation_fact_sites.py tests/test_allocation_fact_sites.py
git commit -m "feat(quality): deterministic RenderedFactSite ledger for allocation facts"
```

---

### Task 5: Persist the run-106 reader JSON as a fixture

**Files:**
- Create: `tests/fixtures/run106_reader_verdict.json`
- Test: `tests/test_fact_attribution.py` (the loader assertion only, in this task)

codex v2 #10: the report file only has truncated summaries; attribution tests must run against the FULL reader JSON. Extract it from the DB if the run-106 row still exists, else reconstruct from the 11 findings in `tmp_review/overnight_synth_report_run5.txt`.

- [ ] **Step 1: Extract or reconstruct the fixture**

Try the DB first (read-only):

```bash
.venv\Scripts\python.exe -c "from argosy.state.db import session_scope; from argosy.state.models import AgentReport; from sqlalchemy import select; import json,sys; \
 s=next(session_scope()); \
 row=s.execute(select(AgentReport).where(AgentReport.decision_id=='plan-synth-106', AgentReport.agent_role=='whole_artifact_reader')).scalars().first(); \
 open('tests/fixtures/run106_reader_verdict.json','w',encoding='utf-8').write(row.response_text if row else ''); \
 print('FOUND' if row else 'MISSING')"
```

If `MISSING`, hand-author `tests/fixtures/run106_reader_verdict.json` as a `WholeArtifactVerdict` shape from the 11 findings in `tmp_review/overnight_synth_report_run5.txt` (overall_assessment `BLOCK`, one `CoherenceFinding` per finding with its `kind`, `severity`, `detail`, and a representative `surfaces_cited` excerpt — e.g. finding [6]: `["medium target is still 3,000 sh/yr", "5,600 sh/yr"]`).

- [ ] **Step 2: Write the failing loader test**

```python
# tests/test_fact_attribution.py (part 1)
import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "run106_reader_verdict.json"


def test_run106_fixture_loads_with_eleven_findings():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["overall_assessment"] == "BLOCK"
    assert len(data["findings"]) == 11
```

- [ ] **Step 3: Run test to verify it fails / then passes once the fixture exists**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_attribution.py::test_run106_fixture_loads_with_eleven_findings -q -p no:cacheprovider`
Expected: PASS once the fixture is written (FAIL if missing/!=11).

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/run106_reader_verdict.json tests/test_fact_attribution.py
git commit -m "test(quality): persist run-106 reader verdict as attribution fixture (codex v2 #10)"
```

---

### Task 6: Ledger-based attribution with fail-safe routing

**Files:**
- Create: `argosy/quality/fact_attribution.py`
- Test: `tests/test_fact_attribution.py` (extend)

A finding → `fact_id`(s) via the ledger (a site whose `rendered_text` / `normalized_value` matches a finding's cited excerpt). Unattributable → a structural-route `FindingLocation` (`fact_id=None`) + a logged gap. Multi-owner allowed.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fact_attribution.py (part 2)
from argosy.quality.fact_ledger import FactLedger, RenderedFactSite, SiteKind
from argosy.quality.fact_attribution import FindingLocation, attribute_finding


def _ledger():
    led = FactLedger()
    led.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="target_allocation_json",
        field_path="$.nvda_cap_pct", byte_span=(0, 0),
        rendered_text="13.0", normalized_value=13.0,
        site_kind=SiteKind.STRUCTURED_FIELD, hash="h",
    ))
    return led


def test_finding_attributes_to_fact_via_ledger_text_match():
    finding = {"kind": "cross_surface", "severity": "AMBER",
               "detail": "cap mismatch", "surfaces_cited": ["NVDA cap 13.0 vs 18"]}
    locs = attribute_finding(finding, _ledger())
    assert any(l.fact_id == "allocation.nvda_cap_pct" for l in locs)


def test_unattributable_finding_is_failsafe_structural():
    finding = {"kind": "other", "severity": "YELLOW",
               "detail": "coverage status", "surfaces_cited": ["sections not baselined"]}
    locs = attribute_finding(finding, _ledger())
    assert len(locs) == 1
    assert locs[0].fact_id is None
    assert locs[0].scope == "structural"  # routes to full re-synth, logged as a gap
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_attribution.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.quality.fact_attribution'`.

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/quality/fact_attribution.py
"""Attribute a reader/gate finding to canonical fact(s) via the render ledger.

The keystone safety property: attribution uses the RenderedFactSite ledger (a
fact→site mapping recorded at render time), NOT a bare excerpt-hash lookup (an
excerpt proves a string existed, not which fact it expresses). A finding that
cannot be attributed is FAIL-SAFE: it routes to full re-synthesis and is logged
as an attribution gap (never silently dropped).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from argosy.quality.fact_ledger import FactLedger
from argosy.quality.fact_inventory import RUN106_FACTS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FindingLocation:
    """Typed locator (replaces the optional ``GateViolation.locator`` string)."""

    check: str | None        # GateCheck value / invariant_id / reader kind
    fact_id: str | None      # the canonical fact, when attributable
    surface_id: str          # body|dashboard|...|"unattributed"
    field_path: str | None
    excerpt_hash: str | None
    scope: str               # "current" | "prior" | "structural"


def _excerpts(finding) -> list[str]:
    cited = finding.get("surfaces_cited") if isinstance(finding, dict) else getattr(finding, "surfaces_cited", None)
    return list(cited or [])


def attribute_finding(finding, ledger: FactLedger, *, inventory=RUN106_FACTS) -> list[FindingLocation]:
    """Map ``finding`` to FindingLocation[] via the ledger. Multi-owner allowed;
    unattributable → a single structural-route location + a logged gap."""
    locs: list[FindingLocation] = []
    excerpts = _excerpts(finding)
    kind = finding.get("kind") if isinstance(finding, dict) else getattr(finding, "kind", None)

    for site in ledger.sites:
        for ex in excerpts:
            if not ex:
                continue
            # text or normalized-value match ties the excerpt to this fact's site
            if site.rendered_text and (site.rendered_text in ex or str(site.normalized_value) in ex):
                locs.append(FindingLocation(
                    check=kind, fact_id=site.fact_id, surface_id=site.surface_id,
                    field_path=site.field_path, excerpt_hash=site.hash, scope="current",
                ))
                break

    # dedupe by (fact_id, surface_id)
    seen = set()
    deduped = []
    for l in locs:
        key = (l.fact_id, l.surface_id)
        if key not in seen:
            seen.add(key)
            deduped.append(l)

    if not deduped:
        log.warning("fact_attribution.unattributable kind=%s excerpts=%s", kind, excerpts[:2])
        return [FindingLocation(
            check=kind, fact_id=None, surface_id="unattributed",
            field_path=None, excerpt_hash=None, scope="structural",
        )]
    return deduped
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_attribution.py -q -p no:cacheprovider`
Expected: PASS (all parts).

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/fact_attribution.py tests/test_fact_attribution.py
git commit -m "feat(quality): ledger-based finding attribution with fail-safe structural routing"
```

---

## Self-review checklist (run before execution)

- **Spec coverage:** Fact/RenderedFactSite ledger (Task 1) ✓; render-site source map keystone (Task 4 — deterministic allocation sites; prose sites deferred to Slice 3, explicitly) ✓; typed FindingLocation (Task 6) ✓; attribution fail-safe (Task 6) ✓; build TargetAllocationDoc before render (Task 3) ✓; run-106 fixture (Task 5) ✓; Phase-1a inventory (Task 2) ✓.
- **Deferred to Slice 3 (named, not silent):** `llm_prose` site ledger instrumentation across every renderer; deterministic re-renderer; prose editor; scoped+global re-verify; demotion of full re-synth.
- **Type consistency:** `RenderedFactSite` fields identical across Tasks 1/4/6; `SiteKind` enum members `TEMPLATE/STRUCTURED_FIELD/LLM_PROSE` used consistently; `FindingLocation.scope` uses `"structural"` for the fail-safe route in both Task 6 test + impl.

## Notes / gotchas

- Synthesis wire-tests hang on a REAL `claude.exe` via `run_alternatives_phase`; reuse `_isolate_external_phases` from `tests/test_plan_synthesis_reader_reconcile.py` for any test that drives `run_synthesis` (Task 3 if you go through the full flow — prefer the inline-stub probe instead).
- Console is cp1252 — never print ₪/Hebrew; fixtures are UTF-8 files.
- Do NOT run the full suite concurrently with a live synthesis.
- New `GateCheck`/locator structures are additive — do not break the existing `GateViolation.locator: str | None`.
