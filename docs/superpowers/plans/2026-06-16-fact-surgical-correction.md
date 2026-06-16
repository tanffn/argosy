# Fact-Level Surgical Correction Implementation Plan (Slice 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the machinery to fix a finding at its **canonical fact + every render site at once** — deterministic re-render for `template`/`structured_field` sites, a cheap prose-editor LLM for `llm_prose` sites — then re-verify the touched fact bundle PLUS the full deterministic suite, and wire it as a surgical PRE-PASS in the reconcile loop (behind a default-OFF flag, since full re-synth must NOT be demoted until the invariant graph covers all 11 run-106 classes — [8]–[10] are still deferred).

**Architecture:** New pure modules in `argosy/quality/` (`fact_correction.py`) + an injectable prose-editor agent (`argosy/agents/prose_editor.py`). `fact_correction` exposes: a deterministic re-renderer (fact + new value + ledger → per-site corrected text), a text-patcher (apply corrections to the artifact), a finding router (surgical vs structural, via Slice-2 attribution), and a re-verify wrapper (runs `gate_plan_output` globally + asserts the touched fact bundle is clean). The orchestrator gains a `_surgical_reconcile_prepass` that runs BEFORE full re-synth when `ARGOSY_SURGICAL_CORRECTION=1`; default OFF keeps live synthesis on the proven full-resynth path. The whole-artifact reader stays the final net.

**Tech Stack:** Python 3.12, dataclasses, pytest. Builds on Slice 2 (`argosy.quality.fact_ledger`, `fact_inventory`, `fact_attribution`, `argosy.services.allocation_fact_sites`). Windows PowerShell (`;` not `&&`). Interpreter `D:/Projects/financial-advisor/.venv/Scripts/python.exe`. Tests: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" <path> -q -p no:cacheprovider`.

**Scope boundary (explicit):** This slice builds + wires the correction machinery behind a default-OFF flag. It does NOT flip the flag on in live synthesis, and does NOT remove the full-resynth reconcile loop or the whole-artifact reader — both stay (spec "Out of scope": demotion waits until the fact-invariant graph provably covers the run-106 classes, which still needs the deferred [8]–[10] invariants). Activation is a later, separately-validated step.

---

## File Structure

- **Create** `argosy/quality/fact_correction.py`:
  - `CorrectionPatch` dataclass (`site`, `new_text`).
  - `rerender_deterministic_sites(fact_id, new_value, ledger) -> list[CorrectionPatch]` — re-render every `template`/`structured_field` site of a fact from the canonical value; `llm_prose` sites are skipped (they route to the editor).
  - `apply_text_corrections(artifact_text, patches, prose_edits) -> str` — apply deterministic patches (by `rendered_text` replacement) + prose edits (offending→corrected) to the assembled text.
  - `route_finding(finding, ledger) -> Literal["surgical","structural"]` — uses Slice-2 `attribute_finding`; surgical iff every location has a `fact_id` (renderable), else structural.
  - `reverify_corrected(corrected_text, *, gate_kwargs) -> GateVerdict` — run `gate_plan_output` globally on the corrected artifact (the touched-bundle + whole-suite re-check).
- **Create** `argosy/agents/prose_editor.py` — `correct_prose_site(*, fact_id, canonical_value, offending_text, editor=None) -> str`: hands a cheap LLM ONLY the fact + canonical value + offending snippet; returns a minimal corrected snippet. `editor` is injectable (stub in tests; default = a thin claude_code call).
- **Modify** `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` — add `_surgical_reconcile_prepass(...)` and call it inside the reconcile block (after `_reader_guidance` extraction, line ~1097, BEFORE the full re-synth at line ~1109) when `ARGOSY_SURGICAL_CORRECTION=1`.
- **Tests:** `tests/test_fact_correction.py`, `tests/test_prose_editor.py`, `tests/test_surgical_reconcile_prepass.py`.

Why these boundaries: the correction primitives are pure/injectable and fully unit-testable; the prose editor isolates the one LLM call; the orchestrator change is one guarded pre-pass that defaults off.

---

### Task 1: Deterministic re-renderer for a fact's sites

**Files:**
- Create: `argosy/quality/fact_correction.py`
- Test: `tests/test_fact_correction.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fact_correction.py
from argosy.quality.fact_ledger import FactLedger, RenderedFactSite, SiteKind
from argosy.quality.fact_correction import CorrectionPatch, rerender_deterministic_sites


def _ledger():
    led = FactLedger()
    led.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="target_allocation_json",
        field_path="$.nvda_cap_pct", byte_span=(0, 0),
        rendered_text="13.0", normalized_value=13.0,
        site_kind=SiteKind.STRUCTURED_FIELD, hash="h1",
    ))
    led.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="body",
        field_path="long#cap", byte_span=(0, 0),
        rendered_text="NVDA cap 13%", normalized_value=13.0,
        site_kind=SiteKind.TEMPLATE, hash="h2",
    ))
    led.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="body",
        field_path="long#prose", byte_span=(0, 0),
        rendered_text="we keep NVDA near the cap", normalized_value=13.0,
        site_kind=SiteKind.LLM_PROSE, hash="h3",
    ))
    return led


def test_rerender_updates_template_and_structured_sites_only():
    patches = rerender_deterministic_sites("allocation.nvda_cap_pct", 18.0, _ledger())
    # structured_field + template re-rendered; llm_prose skipped
    kinds = {p.site.site_kind for p in patches}
    assert SiteKind.LLM_PROSE not in kinds
    assert len(patches) == 2
    # the new text carries the new canonical value
    json_patch = next(p for p in patches if p.site.surface_id == "target_allocation_json")
    assert "18" in json_patch.new_text
    body_patch = next(p for p in patches if p.site.field_path == "long#cap")
    assert "18" in body_patch.new_text and "13" not in body_patch.new_text


def test_rerender_unknown_fact_returns_empty():
    assert rerender_deterministic_sites("nope", 1.0, _ledger()) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_correction.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.quality.fact_correction'`.

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/quality/fact_correction.py
"""Fact-level surgical correction — fix a finding at its canonical fact + every
render site at once, so the contradiction cannot move.

- ``template`` / ``structured_field`` sites are produced FROM the canonical
  value, so they re-render deterministically when the fact changes.
- ``llm_prose`` sites are authored free text; they route to the prose editor.

Re-verify runs the FULL deterministic suite (global, by design — FI-shock and
coherence read artifact-wide), then the whole-artifact reader stays as the net.
Pure functions except ``reverify_corrected`` which calls the deterministic gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from argosy.quality.fact_ledger import FactLedger, RenderedFactSite, SiteKind


@dataclass(frozen=True)
class CorrectionPatch:
    """A deterministic re-render of one site from the canonical value."""

    site: RenderedFactSite
    new_text: str


def _retext(old_text: str, old_value: Any, new_value: Any) -> str:
    """Replace the old value token in a site's rendered_text with the new one,
    tolerating int/float formatting ('13' / '13.0')."""
    new_s = _fmt(new_value)
    out = old_text
    for token in {_fmt(old_value), str(old_value)}:
        if token and token in out:
            out = out.replace(token, new_s)
    return out


def _fmt(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def rerender_deterministic_sites(
    fact_id: str, new_value: Any, ledger: FactLedger
) -> list[CorrectionPatch]:
    """Re-render every template/structured_field site of ``fact_id`` from the new
    canonical value. llm_prose sites are skipped (they go to the prose editor)."""
    patches: list[CorrectionPatch] = []
    for site in ledger.sites_for_fact(fact_id):
        if site.site_kind == SiteKind.LLM_PROSE:
            continue
        new_text = _retext(site.rendered_text, site.normalized_value, new_value)
        patches.append(CorrectionPatch(site=site, new_text=new_text))
    return patches
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_correction.py -q -p no:cacheprovider`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/fact_correction.py tests/test_fact_correction.py
git commit -m "feat(quality): deterministic re-renderer for a fact's template/structured sites"
```

---

### Task 2: Apply corrections to the artifact text

**Files:**
- Modify: `argosy/quality/fact_correction.py`
- Test: `tests/test_fact_correction.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_fact_correction.py
from argosy.quality.fact_correction import apply_text_corrections


def test_apply_corrections_replaces_deterministic_and_prose_text():
    artifact = "NVDA cap 13% in the body. We keep NVDA near the cap of 13%."
    patches = rerender_deterministic_sites("allocation.nvda_cap_pct", 18.0, _ledger())
    # deterministic: 'NVDA cap 13%' -> 'NVDA cap 18%' (template site rendered_text match)
    prose_edits = [("We keep NVDA near the cap of 13%.",
                    "We keep NVDA near the cap of 18%.")]
    out = apply_text_corrections(artifact, patches, prose_edits)
    assert "NVDA cap 18%" in out
    assert "near the cap of 18%" in out
    assert "13%" not in out


def test_apply_corrections_no_ops_when_text_absent():
    # a patch whose rendered_text isn't in the artifact is skipped, not an error
    artifact = "unrelated text"
    patches = rerender_deterministic_sites("allocation.nvda_cap_pct", 18.0, _ledger())
    assert apply_text_corrections(artifact, patches, []) == "unrelated text"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_correction.py::test_apply_corrections_replaces_deterministic_and_prose_text -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'apply_text_corrections'`.

- [ ] **Step 3: Write minimal implementation** (append to `fact_correction.py`)

```python
def apply_text_corrections(
    artifact_text: str,
    patches: list[CorrectionPatch],
    prose_edits: list[tuple[str, str]],
) -> str:
    """Apply deterministic patches (replace each site's old rendered_text with
    its new_text) + prose edits (offending→corrected) to the assembled artifact.

    Text-replacement based (the render sites are raw markdown segments, not
    offset-addressable structured fields — see the render.py prose sites). A
    patch/edit whose source text is absent is skipped (idempotent, never raises).
    """
    out = artifact_text or ""
    for p in patches:
        if p.site.rendered_text and p.site.rendered_text in out:
            out = out.replace(p.site.rendered_text, p.new_text)
    for old, new in prose_edits or []:
        if old and old in out:
            out = out.replace(old, new)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_correction.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/fact_correction.py tests/test_fact_correction.py
git commit -m "feat(quality): apply deterministic + prose corrections to the assembled artifact"
```

---

### Task 3: Surgical-vs-structural finding router

**Files:**
- Modify: `argosy/quality/fact_correction.py`
- Test: `tests/test_fact_correction.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_fact_correction.py
from argosy.quality.fact_correction import route_finding


def test_attributable_finding_routes_surgical():
    finding = {"kind": "cross_surface", "severity": "AMBER",
               "surfaces_cited": ["NVDA cap 13% vs 18"]}
    # ledger has a template site whose rendered_text 'NVDA cap 13%' matches
    assert route_finding(finding, _ledger()) == "surgical"


def test_unattributable_finding_routes_structural():
    finding = {"kind": "other", "severity": "YELLOW",
               "surfaces_cited": ["coverage sections not baselined"]}
    assert route_finding(finding, _ledger()) == "structural"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_correction.py::test_attributable_finding_routes_surgical -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'route_finding'`.

- [ ] **Step 3: Write minimal implementation** (append to `fact_correction.py`)

```python
def route_finding(finding, ledger: FactLedger) -> Literal["surgical", "structural"]:
    """Decide the fix path. A finding whose locations ALL attribute to a concrete
    fact_id is surgically renderable; an unattributable (fact_id=None / structural
    scope) finding routes to full re-synthesis (the derivation, not the rendering,
    is suspect). Uses Slice-2 ledger attribution."""
    from argosy.quality.fact_attribution import attribute_finding

    locs = attribute_finding(finding, ledger)
    if locs and all(loc.fact_id is not None and loc.scope != "structural" for loc in locs):
        return "surgical"
    return "structural"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_correction.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/fact_correction.py tests/test_fact_correction.py
git commit -m "feat(quality): surgical-vs-structural finding router (ledger attribution)"
```

---

### Task 4: Re-verify the corrected artifact (global suite)

**Files:**
- Modify: `argosy/quality/fact_correction.py`
- Test: `tests/test_fact_correction.py` (extend)

The spec is explicit: re-verify the touched fact bundle PLUS the full deterministic suite (it is global — FI-shock + coherence read artifact-wide). A planted cross-fact contradiction must NOT be missed.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_fact_correction.py
from datetime import date
from argosy.quality.fact_correction import reverify_corrected
from argosy.quality.gate_types import GateCheck


def test_reverify_runs_global_suite_and_catches_planted_contradiction():
    # a corrected artifact that still has an IPS prose self-sum != 100 must be
    # caught by the GLOBAL suite (proves re-verify is not section-scoped)
    corrected = (
        "## IPS Instrument Map\nNVDA 13%\nGlobal equity 60%\nGold 18%\nBonds 20%\n"
    )  # sums to 111
    verdict = reverify_corrected(
        corrected, gate_kwargs={"today": date(2026, 6, 16)}
    )
    assert verdict.violations[GateCheck.IPS_EQUALITY]


def test_reverify_clean_artifact_passes_relevant_checks():
    corrected = "All surfaces agree. Nothing contradictory."
    verdict = reverify_corrected(corrected, gate_kwargs={"today": date(2026, 6, 16)})
    # no IPS / coherence violations on clean text
    assert not verdict.violations[GateCheck.IPS_EQUALITY]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_correction.py::test_reverify_runs_global_suite_and_catches_planted_contradiction -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'reverify_corrected'`.

- [ ] **Step 3: Write minimal implementation** (append to `fact_correction.py`)

```python
def reverify_corrected(corrected_text: str, *, gate_kwargs: dict | None = None):
    """Re-run the FULL deterministic gate suite on the corrected artifact.

    The suite is global by design (FI-shock + coherence + IPS read artifact-
    wide), so a surgical patch is re-checked against the WHOLE document, never a
    section. The whole-artifact LLM reader stays as the holistic net downstream;
    this is the deterministic half. ``gate_kwargs`` forwards optional inputs
    (today, snapshot_date, resolved, fx, caps, target_allocation_doc, ...)."""
    from argosy.quality.plan_output_gate import gate_plan_output

    kwargs = dict(gate_kwargs or {})
    # The corrected artifact is one text blob; feed it as the long horizon so the
    # prose-scanning invariants (IPS, taxonomy, timeline, etc.) run over it.
    return gate_plan_output(horizon_text={"long": corrected_text}, **kwargs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_fact_correction.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/fact_correction.py tests/test_fact_correction.py
git commit -m "feat(quality): re-verify corrected artifact against the full global gate suite"
```

---

### Task 5: The prose-editor agent (injectable LLM)

**Files:**
- Create: `argosy/agents/prose_editor.py`
- Test: `tests/test_prose_editor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prose_editor.py
from argosy.agents.prose_editor import correct_prose_site


def test_prose_editor_passes_fact_and_returns_snippet_via_injected_editor():
    captured = {}

    def fake_editor(prompt: str) -> str:
        captured["prompt"] = prompt
        return "We keep NVDA near the 18% cap."

    out = correct_prose_site(
        fact_id="allocation.nvda_cap_pct", canonical_value=18.0,
        offending_text="We keep NVDA near the 13% cap.",
        editor=fake_editor,
    )
    assert out == "We keep NVDA near the 18% cap."
    # the editor is handed ONLY the fact, its canonical value, and the snippet
    assert "allocation.nvda_cap_pct" in captured["prompt"]
    assert "18" in captured["prompt"]
    assert "We keep NVDA near the 13% cap." in captured["prompt"]


def test_prose_editor_returns_original_on_editor_failure():
    def boom(prompt: str) -> str:
        raise RuntimeError("llm down")

    original = "We keep NVDA near the 13% cap."
    out = correct_prose_site(
        fact_id="allocation.nvda_cap_pct", canonical_value=18.0,
        offending_text=original, editor=boom,
    )
    # fail-safe: never raises; returns the original (re-verify will still flag it)
    assert out == original
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_prose_editor.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.agents.prose_editor'`.

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/agents/prose_editor.py
"""Cheap single-fact prose corrector for ``llm_prose`` render sites.

A deterministic re-render cannot fix authored free text (HorizonSection.rationale
/ posture, Action.detail/rationale). This editor is handed ONLY the fact, its
canonical value, and the offending snippet, and returns a MINIMAL corrected
snippet — the smallest edit that makes the prose state the canonical value. It
does not see (or get to rewrite) the rest of the plan.

``editor`` is injectable: tests pass a stub; the default is a thin claude_code
call. Fail-safe — any editor error returns the original text unchanged (the
re-verify pass + whole-artifact reader remain the backstop)."""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

_PROMPT = """You are correcting ONE factual value in a snippet of an existing financial plan.

Canonical fact: {fact_id}
Correct value: {value}

Offending snippet (it states a WRONG or stale value for this fact):
\"\"\"{snippet}\"\"\"

Return ONLY the corrected snippet — the SAME wording, with just the value fixed
to the canonical value above. Do not add commentary, caveats, or new sentences.
"""


def _default_editor(prompt: str) -> str:
    """Thin claude_code dispatch (kept tiny; the real call is wired lazily so the
    module imports without a backend)."""
    from argosy.llm.claude_code import run_text_prompt  # lazy — see SDD §3.8

    return run_text_prompt(prompt, max_tokens=400)


def correct_prose_site(
    *,
    fact_id: str,
    canonical_value: object,
    offending_text: str,
    editor: Callable[[str], str] | None = None,
) -> str:
    """Return a minimal corrected snippet for an llm_prose site. Fail-safe:
    returns ``offending_text`` unchanged on any editor error."""
    editor = editor or _default_editor
    prompt = _PROMPT.format(fact_id=fact_id, value=canonical_value, snippet=offending_text)
    try:
        out = (editor(prompt) or "").strip()
        return out or offending_text
    except Exception as exc:  # noqa: BLE001 — fail-safe; re-verify is the backstop
        log.warning("prose_editor.failed fact=%s err=%s", fact_id, exc)
        return offending_text
```

> **Implementer note:** verify the default editor's import path. If
> `argosy.llm.claude_code` has no `run_text_prompt`, find the project's existing
> thin text-dispatch helper (grep `def run_` in `argosy/llm/`) and use that; the
> default editor is only exercised live, never in tests (tests inject `editor`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_prose_editor.py -q -p no:cacheprovider`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add argosy/agents/prose_editor.py tests/test_prose_editor.py
git commit -m "feat(agents): injectable single-fact prose editor for llm_prose sites"
```

---

### Task 6: Surgical reconcile pre-pass (wired behind a default-OFF flag)

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py`
- Modify: `argosy/orchestrator/flows/plan_synthesis/__init__.py` (re-export the pre-pass for `_pkg.` + monkeypatching)
- Test: `tests/test_surgical_reconcile_prepass.py`

The pre-pass is the integration. It runs INSIDE the reconcile block, after `_reader_guidance` extraction (orchestrator.py ~line 1097) and BEFORE full re-synth (~line 1109), ONLY when `ARGOSY_SURGICAL_CORRECTION=1` (default OFF). It builds the ledger (allocation sites + any available), routes each fixable finding, deterministically re-renders surgical ones, re-verifies, and — if the deterministic suite is clean — persists the corrected bodies and skips full re-synth for the resolved findings. Structural/unattributable findings still fall through to full re-synth. The whole-artifact reader still runs after. Full re-synth is NOT removed.

- [ ] **Step 1: Write the failing test** (pure helper-level, no full synthesis)

```python
# tests/test_surgical_reconcile_prepass.py
"""The surgical pre-pass corrects a renderable finding deterministically and
re-verifies, without a full re-synthesis. Helper-level test (no live LLM)."""
from __future__ import annotations

from datetime import date

from argosy.quality.fact_ledger import FactLedger, RenderedFactSite, SiteKind
from argosy.orchestrator.flows.plan_synthesis import _surgical_reconcile_prepass


def _ledger():
    led = FactLedger()
    led.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="body",
        field_path="long#cap", byte_span=(0, 0),
        rendered_text="NVDA cap 13%", normalized_value=13.0,
        site_kind=SiteKind.TEMPLATE, hash="h",
    ))
    return led


def test_prepass_corrects_renderable_finding_and_reports_resolved():
    artifact = "NVDA cap 13% in the body and NVDA cap 18% on the dashboard."
    finding = {"kind": "cross_surface", "severity": "BLOCKER",
               "surfaces_cited": ["NVDA cap 13%", "NVDA cap 18%"]}
    result = _surgical_reconcile_prepass(
        artifact_text=artifact,
        findings=[finding],
        ledger=_ledger(),
        canonical_values={"allocation.nvda_cap_pct": 18.0},
        gate_kwargs={"today": date(2026, 6, 16)},
    )
    # the deterministic site was re-rendered to the canonical 18%
    assert "NVDA cap 18% in the body" in result.corrected_text
    assert "allocation.nvda_cap_pct" in result.corrected_fact_ids
    # structural findings that can't be surgically fixed are reported back
    assert result.structural_findings == []


def test_prepass_reports_structural_finding_for_fallback():
    artifact = "coverage sections not baselined"
    finding = {"kind": "other", "severity": "YELLOW",
               "surfaces_cited": ["coverage sections not baselined"]}
    result = _surgical_reconcile_prepass(
        artifact_text=artifact, findings=[finding], ledger=_ledger(),
        canonical_values={}, gate_kwargs={"today": date(2026, 6, 16)},
    )
    assert result.structural_findings == [finding]
    assert result.corrected_fact_ids == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_surgical_reconcile_prepass.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name '_surgical_reconcile_prepass'`.

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` (near the other reader-reconcile helpers, ~line 3083):

```python
from dataclasses import dataclass as _dataclass


@_dataclass
class _SurgicalPrepassResult:
    corrected_text: str
    corrected_fact_ids: list[str]
    structural_findings: list[object]


def _surgical_reconcile_prepass(
    *, artifact_text, findings, ledger, canonical_values, gate_kwargs,
):
    """Fix renderable findings at their canonical fact + render sites BEFORE the
    full-resynth fallback. Deterministic for template/structured sites; structural
    / unattributable findings are returned for the caller to route to re-synth.

    Pure (no LLM here — prose-editor calls are the caller's responsibility for
    llm_prose sites). The whole-artifact reader + full re-synth remain downstream.
    """
    from argosy.quality.fact_correction import (
        apply_text_corrections, rerender_deterministic_sites, route_finding,
    )

    corrected = artifact_text or ""
    corrected_fact_ids: list[str] = []
    structural: list[object] = []

    for finding in findings or []:
        if route_finding(finding, ledger) == "structural":
            structural.append(finding)
            continue
        from argosy.quality.fact_attribution import attribute_finding
        for loc in attribute_finding(finding, ledger):
            fid = loc.fact_id
            if fid is None or fid not in canonical_values:
                continue
            patches = rerender_deterministic_sites(fid, canonical_values[fid], ledger)
            corrected = apply_text_corrections(corrected, patches, prose_edits=[])
            if fid not in corrected_fact_ids:
                corrected_fact_ids.append(fid)

    return _SurgicalPrepassResult(
        corrected_text=corrected,
        corrected_fact_ids=corrected_fact_ids,
        structural_findings=structural,
    )
```

Re-export in `argosy/orchestrator/flows/plan_synthesis/__init__.py` (near the in-stage gate export):

```python
from argosy.orchestrator.flows.plan_synthesis.orchestrator import (  # noqa: F401
    _surgical_reconcile_prepass,
)
```
and add `"_surgical_reconcile_prepass"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_surgical_reconcile_prepass.py -q -p no:cacheprovider`
Expected: PASS (2 passed).

- [ ] **Step 5: Wire the pre-pass into the reconcile loop (guarded, default OFF)**

In `run_synthesis`, inside the reconcile block after `_reader_guidance` is computed (~line 1097) and BEFORE the full re-synth (~line 1109), add:

```python
            # Surgical pre-pass (default OFF — ARGOSY_SURGICAL_CORRECTION=1 to
            # enable). Corrects renderable findings at their canonical fact +
            # render sites; structural ones fall through to full re-synth below.
            # NOT a demotion of full re-synth (spec: stays until the invariant
            # graph covers all run-106 classes — [8]-[10] still deferred).
            if _os.environ.get("ARGOSY_SURGICAL_CORRECTION", "0") == "1":
                try:
                    _pkg._record_phase_completion(
                        user_id=user_id, decision_run_id=decision_run_id,
                        phase_n=54, started_at=datetime.now(timezone.utc),
                        phase_output="surgical_prepass: enabled (experimental)",
                        agent_report_rows=[],
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("plan_synthesis.surgical_prepass_phase_failed err=%s", exc)
```

> **Implementer note:** keep the LIVE wiring minimal in this slice — record the
> phase marker so `/decisions/[id]` shows the pre-pass ran, but do NOT yet
> replace the full-resynth persist with the corrected bodies (that activation is
> gated on full run-106 coverage). The pure pre-pass + its unit test prove the
> mechanism; flipping it to drive the persisted bodies is a follow-up once
> [8]–[10] invariants land. This keeps the default-OFF contract honest.

- [ ] **Step 6: Run the isolated synthesis regression**

Run: `.venv\Scripts\python.exe -m pytest -m "not llm_eval" tests/test_plan_synthesis_instage_gate.py tests/test_plan_synthesis_reader_reconcile.py tests/test_surgical_reconcile_prepass.py -q -p no:cacheprovider`
Expected: PASS (the default-OFF flag means the live loop is unchanged; the pre-pass unit test is green).

- [ ] **Step 7: Commit**

```bash
git add argosy/orchestrator/flows/plan_synthesis/orchestrator.py argosy/orchestrator/flows/plan_synthesis/__init__.py tests/test_surgical_reconcile_prepass.py
git commit -m "feat(synthesis): surgical reconcile pre-pass (default OFF; full re-synth retained)"
```

---

## Self-review checklist (run before execution)

- **Spec coverage:** deterministic re-render of template/structured sites (Task 1) ✓; prose editor for llm_prose (Task 5) ✓; apply corrections (Task 2) ✓; surgical-vs-structural routing (Task 3) ✓; re-verify touched bundle + FULL global suite + reader retained (Task 4 + Task 6 note) ✓; full re-synth NOT demoted (Task 6 default-OFF + note) ✓.
- **Deferred / gated (named, not silent):** flipping `ARGOSY_SURGICAL_CORRECTION` on to drive persisted bodies — gated on the deferred [8]–[10] invariants completing the run-106 coverage graph (spec "Out of scope"). The whole-artifact reader + full re-synth stay.
- **Type consistency:** `CorrectionPatch(site, new_text)` used identically in Tasks 1/2/6; `route_finding` returns the same `"surgical"|"structural"` literal in Task 3 + Task 6; `_SurgicalPrepassResult` fields (`corrected_text`, `corrected_fact_ids`, `structural_findings`) match the Task 6 test.

## Notes / gotchas

- Synthesis wire-tests hang on a REAL `claude.exe` via `run_alternatives_phase` UNLESS `_isolate_external_phases` is applied; the pre-pass unit test (Task 6) is helper-level and avoids the full flow entirely — prefer that.
- `test_plan_synthesis_whole_artifact.py` run STANDALONE hangs (alternatives-phase isolation only applies when its fixtures are co-collected); run the instage + reader-reconcile tests for the synthesis regression instead.
- Console is cp1252 — never print ₪/Hebrew; fixtures are UTF-8.
- Do NOT run the full suite concurrently with a live synthesis.
- The prose editor's default dispatch must import lazily so the module loads without a backend; tests always inject `editor`.
