# Generator-swap M1 — graph → plan_version render bridge

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Render a full, coherent plan-version artifact (sections_json + horizon markdown) FROM a
hydrated+edited derivation graph — the inverse of `graph_hydration.add_surface_nodes` — so the
incremental path produces something publishable. No live wiring; pure + deterministic.

**Architecture:** New module `argosy/quality/graph_to_plan.py`. Surface nodes are keyed
`surface:<horizon>:<section_id>` (carry edited `body_md`); the renderer walks the base section
list, replaces each body with the graph's current value for its surface node, and re-serializes
sections_json. Canonical numeric surfaces (`live_surfaces`) are reflected by a later task via the
existing `surface_rendering` reconcile. Pure functions only — no DB, no LLM.

**Tech Stack:** Python, `argosy.agents.plan_synthesizer_types.Section`, `argosy.quality.derivation_graph`.

---

### Task 1: Render sections_json from the graph's surface nodes

**Files:**
- Create: `argosy/quality/graph_to_plan.py`
- Test: `tests/test_graph_to_plan.py`

- [ ] **Step 1: Write the failing test**

```python
import json
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.graph_to_plan import render_sections_json_from_graph


def _section(section_id, horizon, body):
    return {"section_id": section_id, "horizon": horizon, "title": "t",
            "body_md": body, "evidence": {"source_span": []}}


def test_render_uses_graph_body_for_each_surface():
    base = [_section("posture", "long", "OLD body"),
            _section("vest", "medium", "OLD vest")]
    g = DerivationGraph()
    g.add_node(Node(key="surface:long:posture", kind=NodeKind.SURFACE,
                    value="NEW body", input_hash="x"))
    g.add_node(Node(key="surface:medium:vest", kind=NodeKind.SURFACE,
                    value="NEW vest", input_hash="x"))
    out = json.loads(render_sections_json_from_graph(g, base))
    bodies = {(s["section_id"], s["horizon"]): s["body_md"] for s in out}
    assert bodies[("posture", "long")] == "NEW body"
    assert bodies[("vest", "medium")] == "NEW vest"
    # non-body fields preserved
    assert out[0]["title"] == "t"


def test_render_keeps_base_body_when_surface_absent_or_invalid():
    base = [_section("posture", "long", "BASE body")]
    g = DerivationGraph()  # no surface node for it
    out = json.loads(render_sections_json_from_graph(g, base))
    assert out[0]["body_md"] == "BASE body"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -m "not llm_eval" -p no:cacheprovider -q tests/test_graph_to_plan.py`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement the minimal renderer**

```python
"""Render plan-version fields FROM a derivation graph — inverse of
graph_hydration.add_surface_nodes. Pure: no DB, no LLM."""
from __future__ import annotations

import json
from typing import Any

from argosy.quality.derivation_graph import DerivationGraph, NodeKind


def _surface_key(horizon: str, section_id: str) -> str:
    return f"surface:{horizon}:{section_id}"


def render_sections_json_from_graph(
    graph: DerivationGraph, base_sections: list[dict[str, Any]]
) -> str:
    """Return sections_json (JSON list) with each section's body_md replaced by
    the graph's VALID surface-node value; base body kept when the surface is
    absent or invalid (fail-safe: never emit an un-derived edit)."""
    keys = set(graph.keys())
    out: list[dict[str, Any]] = []
    for sec in base_sections:
        s = dict(sec)
        k = _surface_key(str(sec.get("horizon", "")), str(sec.get("section_id", "")))
        if k in keys and graph.is_valid(k):
            val = graph.get(k).value
            if isinstance(val, str) and val:
                s["body_md"] = val
        out.append(s)
    return json.dumps(out, ensure_ascii=False)


__all__ = ["render_sections_json_from_graph"]
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -m "not llm_eval" -p no:cacheprovider -q tests/test_graph_to_plan.py`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/graph_to_plan.py tests/test_graph_to_plan.py
git commit -m "feat(graph): render sections_json from the graph's surface nodes (M1 render bridge)"
```

---

### Task 2: Round-trip property — hydrate(plan) → graph → render == plan (bodies)

**Files:**
- Modify: `argosy/quality/graph_to_plan.py`
- Test: `tests/test_graph_to_plan.py`

- [ ] **Step 1: Write the failing round-trip test** — build a graph via
  `graph_hydration.add_surface_nodes` from real `Section` objects + `recompute_safe`, then assert
  `render_sections_json_from_graph` reproduces every section body_md unchanged (proves the inverse
  is faithful — the migration "VERIFY, not just reproduce" foundation).

```python
def test_roundtrip_hydrate_then_render_preserves_bodies():
    from argosy.agents.plan_synthesizer_types import Citation, Section, SectionEvidence, FactClaim
    from argosy.quality.derivation_graph import DerivationGraph
    from argosy.quality.graph_hydration import add_surface_nodes, recompute_safe
    secs = [Section(section_id="posture", horizon="long", title="t",
                    body_md="long posture body",
                    evidence=SectionEvidence(fact_claims=[],
                        source_span=[Citation(source_kind="analyst_report", source_id="a", source_locator="x")]))]
    g = DerivationGraph()
    add_surface_nodes(g, secs)
    recompute_safe(g)
    base = [{"section_id": "posture", "horizon": "long", "title": "t",
             "body_md": "long posture body", "evidence": {"source_span": []}}]
    import json
    out = json.loads(render_sections_json_from_graph(g, base))
    assert out[0]["body_md"] == "long posture body"
```

- [ ] **Step 2-4:** run (should already PASS with Task-1 impl since the echo recipe reproduces
  body_md); if the SectionEvidence constructor differs, adjust the test's construction to match
  `argosy/agents/plan_synthesizer_types.py` (read it first). Expected: PASS.

- [ ] **Step 5: Commit** `test(graph): round-trip hydrate->render preserves section bodies`

---

### Task 3: Assemble full plan_version field dict (horizon markdown + sections_json)

**Files:**
- Modify: `argosy/quality/graph_to_plan.py`
- Test: `tests/test_graph_to_plan.py`

- [ ] **Step 1: Write the failing test** for `render_plan_fields_from_graph(graph, base_sections)
  -> dict` returning `{"sections_json": <json>, "horizon_long_md": <md>, "horizon_medium_md":
  <md>, "horizon_short_md": <md>}` where each `horizon_<h>_md` is the concatenation (in section
  order) of that horizon's section bodies joined by `\n\n`. Assert the long-horizon md contains
  the long sections' bodies and not the medium ones.

- [ ] **Step 2: Implement** `render_plan_fields_from_graph` reusing
  `render_sections_json_from_graph`, then grouping rendered sections by horizon and joining bodies.

- [ ] **Step 3-4:** run; Expected PASS.

- [ ] **Step 5: Commit** `feat(graph): assemble full plan_version field dict from the graph`

---

### Task 4 (checkpoint): reflect canonical numeric surfaces in prose

This task depends on a design call (surgical reconcile vs. dedicated number lines) and is the
boundary to M2. STOP after Task 3 and checkpoint: decide whether the canonical numbers
(`live_surfaces`) are injected into prose via `surface_rendering` surgical reconcile, or rendered
as a dedicated canonical-numbers block the prose references. Do NOT guess — surface to the user.
