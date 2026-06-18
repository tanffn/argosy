# Graph Surfaces (Phase 1d) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render SURFACE nodes (dashboard tiles, appendix tables, the FI verdict) deterministically from their inbound derived-value nodes so two surfaces can never disagree about the same fact, route free-text prose surfaces through the existing `surgical_reconcile` editor (span-local, no new numbers), and run a cheap deterministic coherence recheck over the whole rendered artifact after every change.

**Architecture:** A new module `argosy/quality/surface_rendering.py` sits ON TOP of the Phase-1a engine (`argosy/quality/derivation_graph.py` — `DerivationGraph`, `Node`, `NodeKind`, `add_node`/`get`/`set_input`/`recompute`/`is_closed`). Deterministic surfaces are `NodeKind.SURFACE` nodes whose `recipe(inbound) -> str` formats their inbound derived-value nodes into text; because N surfaces over the SAME `fi_margin` node read ONE value, recomputing the graph after a `set_input` re-renders ALL of them identically — a basis-flip is impossible by construction. A `ProseSurface` is a thin adapter that drives the existing `argosy.orchestrator.flows.plan_synthesis.surgical_reconcile.surgically_correct_draft` from a prose surface node's recipe, inheriting its span-local, no-new-numbers guarantee. After recompute we run `argosy.quality.coherence_gate.check_cross_surface_coherence` over an `AssembledArtifact`-shaped `surface_values` map built from the rendered SURFACE nodes — the per-change cheap coherence backstop (NOT the promotion gate).

**Tech Stack:** Python 3.12, stdlib + the existing `argosy.quality` / `argosy.orchestrator.flows.plan_synthesis` / `argosy.services` modules. pytest (`-m "not llm_eval"`). No DB / no LLM in tests — the engine is pure and the prose editor is injectable (`editor=` stub).

**Out of scope (other phases / plans):** the engine itself (Phase 1a — `derivation_graph.py`, already specced); hydrating the graph from `plan_numeric_resolver` / `sections_json` (Phase 1b/1c); SQLAlchemy persistence + the `propagation_events` / `dialogue_turns` Replay trace; the change/adjudication substrate + negotiation ladder (Phase 2); the full `promote_gate` promotion authority. This plan delivers SURFACE rendering + the per-change coherence recheck only.

---

### Task 1: SurfaceRenderError + a deterministic SURFACE node factory

**Files:**
- Create: `argosy/quality/surface_rendering.py`
- Test: `tests/test_surface_rendering.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_surface_rendering.py
import pytest

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.surface_rendering import (
    SurfaceRenderError,
    make_surface_node,
)


def test_make_surface_node_builds_a_surface_kind_node():
    node = make_surface_node(
        key="surface:fi_tile",
        inputs=("retirement.fi_margin_signed_nis",),
        recipe=lambda inbound: f"margin {inbound['retirement.fi_margin_signed_nis']}",
        compute_version="tile-v1",
    )
    assert node.kind is NodeKind.SURFACE
    assert node.key == "surface:fi_tile"
    assert node.inputs == ("retirement.fi_margin_signed_nis",)
    assert node.compute_version == "tile-v1"


def test_surface_node_renders_from_its_inbound_value():
    g = DerivationGraph()
    g.add_node(Node(key="retirement.fi_margin_signed_nis", kind=NodeKind.INPUT, value=-148_208.0))
    g.add_node(make_surface_node(
        key="surface:fi_tile",
        inputs=("retirement.fi_margin_signed_nis",),
        recipe=lambda inbound: f"margin {inbound['retirement.fi_margin_signed_nis']:.0f}",
        compute_version="tile-v1",
    ))
    g.recompute()
    assert g.get("surface:fi_tile").value == "margin -148208"


def test_make_surface_node_rejects_a_non_callable_recipe():
    with pytest.raises(SurfaceRenderError):
        make_surface_node(key="surface:x", inputs=("a",), recipe=None, compute_version="v1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.quality.surface_rendering'`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/quality/surface_rendering.py
"""Phase 1d — render SURFACE nodes from their inbound derived-value nodes.

A deterministic-render surface (dashboard tile, appendix table, the FI verdict)
is a ``NodeKind.SURFACE`` node whose ``recipe(inbound) -> str`` formats the
values of its inbound DERIVED-value nodes into text. Because N surfaces over the
SAME derived node all read one value, recomputing the graph after a
``set_input`` re-renders ALL of them identically — two surfaces CANNOT disagree
about the same fact (a basis-flip is impossible by construction).

Free-text prose surfaces do NOT re-render from scratch; they reuse the existing
converge-safe ``surgical_reconcile`` editor (span-local, forbidden from
introducing new numbers) driven by the prose surface node's recipe.

After a change is applied + propagated, a cheap deterministic coherence recheck
runs over the WHOLE rendered artifact (``coherence_gate.check_cross_surface_
coherence``) — the per-change backstop, NOT the promotion gate.

See docs/superpowers/specs/2026-06-18-living-plan-derivation-graph-design.md
(Layer 3 steps 4 + 6).
"""
from __future__ import annotations

from typing import Any, Callable

from argosy.quality.derivation_graph import Node, NodeKind

# A deterministic surface recipe maps {inbound_key: value} -> rendered text.
SurfaceRecipe = Callable[[dict[str, Any]], str]


class SurfaceRenderError(Exception):
    """Raised when a surface node is mis-declared (no recipe, no inputs)."""


def make_surface_node(
    *,
    key: str,
    inputs: tuple[str, ...],
    recipe: SurfaceRecipe | None,
    compute_version: str,
) -> Node:
    """Build a deterministic-render ``NodeKind.SURFACE`` node.

    The node renders by calling ``recipe`` with the {inbound_key: value} dict the
    engine assembles from ``inputs``. ``compute_version`` is the render-template
    version folded into the engine's ``input_hash`` so a template change
    invalidates the surface even when its inbound values are unchanged.
    """
    if recipe is None or not callable(recipe):
        raise SurfaceRenderError(f"surface {key!r} needs a callable recipe")
    if not inputs:
        raise SurfaceRenderError(f"surface {key!r} declares no inbound nodes")
    return Node(
        key=key,
        kind=NodeKind.SURFACE,
        inputs=tuple(inputs),
        recipe=recipe,
        compute_version=compute_version,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/surface_rendering.py tests/test_surface_rendering.py
git commit -m "feat(surfaces): SurfaceRenderError + deterministic SURFACE node factory"
```

---

### Task 2: One derived node feeds N surfaces; a change updates ALL identically (no basis-flip)

**Files:**
- Modify: `argosy/quality/surface_rendering.py`
- Test: `tests/test_surface_rendering.py`

- [ ] **Step 1: Write the failing test**

This is the spec's headline guarantee: a single FI-margin node feeds N surfaces and changing its input updates all of them identically. We also assert the FI verdict word ("reached" vs "short") flips consistently across every surface — never one surface "reached" and another "short" (the recurring +118,020/−148,208 basis-flip).

```python
from argosy.quality.surface_rendering import (
    render_fi_verdict_text,
    build_fi_margin_surfaces,
)


def _fi_graph(margin_value: float) -> DerivationGraph:
    """A graph where ONE fi_margin DERIVED node feeds three surfaces."""
    g = DerivationGraph()
    # Inputs the margin derives from.
    g.add_node(Node(key="portfolio.liquid_net_worth_nis", kind=NodeKind.INPUT, value=11_687_926.0))
    g.add_node(Node(key="retirement.fi_total_capital_nis", kind=NodeKind.INPUT,
                    value=11_687_926.0 - margin_value))
    # The ONE derived margin node (liquid_nw - total_capital).
    g.add_node(Node(
        key="retirement.fi_margin_signed_nis",
        kind=NodeKind.DERIVED,
        inputs=("portfolio.liquid_net_worth_nis", "retirement.fi_total_capital_nis"),
        recipe=lambda i: i["portfolio.liquid_net_worth_nis"] - i["retirement.fi_total_capital_nis"],
        compute_version="fi-margin-v1",
    ))
    for node in build_fi_margin_surfaces():
        g.add_node(node)
    g.recompute()
    return g


def test_one_margin_node_feeds_all_surfaces_identically_when_short():
    g = _fi_graph(margin_value=-148_208.0)
    tile = g.get("surface:dashboard.fi_tile").value
    table = g.get("surface:appendix.fi_table").value
    verdict = g.get("surface:fi_verdict").value
    # Every surface reads the SAME margin: all say "short", none says "reached".
    assert "short" in verdict.lower()
    assert "reached" not in verdict.lower() or "not" in verdict.lower()
    assert "148,208" in tile or "148208" in tile
    assert "148,208" in table or "148208" in table
    assert "148,208" in verdict or "148208" in verdict


def test_changing_the_input_updates_all_surfaces_with_no_basis_flip():
    g = _fi_graph(margin_value=-148_208.0)
    # Flip the input so the margin becomes POSITIVE (FI reached).
    invalidated = g.set_input("retirement.fi_total_capital_nis", 11_000_000.0)
    # The derived margin + all three surfaces are downstream of the input.
    assert "retirement.fi_margin_signed_nis" in invalidated
    assert {"surface:dashboard.fi_tile", "surface:appendix.fi_table",
            "surface:fi_verdict"} <= invalidated
    g.recompute()
    margin = g.get("retirement.fi_margin_signed_nis").value
    assert margin == pytest.approx(687_926.0)
    # ALL surfaces now say reached — none stuck on "short" (the basis-flip bug).
    verdict = g.get("surface:fi_verdict").value
    tile = g.get("surface:dashboard.fi_tile").value
    table = g.get("surface:appendix.fi_table").value
    assert "reached" in verdict.lower() and "short" not in verdict.lower()
    assert "687,926" in tile or "687926" in tile
    assert "687,926" in table or "687926" in table


def test_render_fi_verdict_text_matches_resolver_doctrine():
    # Negative margin -> NOT reached, states the shortfall amount.
    assert "short" in render_fi_verdict_text(-148_208.0).lower()
    # Positive margin -> REACHED, states the margin amount.
    assert "reached" in render_fi_verdict_text(687_926.0).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py -q`
Expected: FAIL — `ImportError: cannot import name 'render_fi_verdict_text'`

- [ ] **Step 3: Write minimal implementation**

Append to `argosy/quality/surface_rendering.py`. The verdict text mirrors the doctrine already in `plan_numeric_resolver.render_numbers_for_synth` (positive ⇒ "REACHED ... margin"; negative ⇒ "NOT reached — short ₪X"). All three surfaces consume the SAME `retirement.fi_margin_signed_nis` node, so they can't diverge.

```python
def render_fi_verdict_text(margin_signed_nis: float) -> str:
    """The canonical FI-sufficiency VERDICT text, derived from the ONE signed
    margin. Mirrors plan_numeric_resolver.render_numbers_for_synth: >=0 => the
    liquid basis covers the total capital target (REACHED) with that margin;
    <0 => liquid is short of the target (NOT reached) by that amount. The verdict
    word is a pure function of the sign, so every surface that renders from this
    node states the SAME conclusion."""
    m = float(margin_signed_nis)
    if m >= 0:
        return (
            f"FI sufficiency VERDICT: REACHED — liquid net worth covers the total "
            f"capital target with a ₪{m:,.0f} margin."
        )
    return (
        f"FI sufficiency VERDICT: NOT reached — liquid net worth is short "
        f"₪{abs(m):,.0f} of the total capital target."
    )


def build_fi_margin_surfaces() -> list[Node]:
    """Three deterministic surfaces that ALL render from the ONE
    ``retirement.fi_margin_signed_nis`` derived node: the dashboard FI tile, the
    appendix FI table row, and the FI verdict. Because they share one inbound
    node, a change to the margin re-renders all three identically — a
    cross-surface basis-flip ('reached' on one, 'short' on another) is
    impossible by construction (spec Layer-3 step 4)."""
    margin_key = "retirement.fi_margin_signed_nis"
    return [
        make_surface_node(
            key="surface:dashboard.fi_tile",
            inputs=(margin_key,),
            recipe=lambda i: f"FI margin: ₪{i[margin_key]:,.0f}",
            compute_version="fi-tile-v1",
        ),
        make_surface_node(
            key="surface:appendix.fi_table",
            inputs=(margin_key,),
            recipe=lambda i: (
                "| Concept | Value |\n"
                "| --- | --- |\n"
                f"| FI sufficiency margin (liquid − total capital) | ₪{i[margin_key]:,.0f} |"
            ),
            compute_version="fi-table-v1",
        ),
        make_surface_node(
            key="surface:fi_verdict",
            inputs=(margin_key,),
            recipe=lambda i: render_fi_verdict_text(i[margin_key]),
            compute_version="fi-verdict-v1",
        ),
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/surface_rendering.py tests/test_surface_rendering.py
git commit -m "feat(surfaces): one fi_margin node feeds 3 surfaces; change updates all (no basis-flip)"
```

---

### Task 3: Extract `surface_values` from rendered SURFACE nodes (the coherence-gate map)

**Files:**
- Modify: `argosy/quality/surface_rendering.py`
- Test: `tests/test_surface_rendering.py`

The deterministic coherence gate (`coherence_gate.check_cross_surface_coherence`) reads a `surface_values: dict[concept] -> list[(surface_name, value)]` map and flags any concept whose surfaces disagree (>1% or sign flip). To run it as the per-change recheck we must extract that map from the rendered SURFACE nodes. We declare, per surface node, which concept(s) it asserts and the numeric value(s) it rendered — a number parsed back out of the rendered text would be fragile, so each surface carries an explicit `concept_values: dict[concept] -> value` callable over its inbound values.

- [ ] **Step 1: Write the failing test**

```python
from argosy.quality.surface_rendering import (
    SurfaceConcept,
    register_surface_concepts,
    extract_surface_values,
)


def test_extract_surface_values_groups_by_concept():
    g = _fi_graph(margin_value=-148_208.0)
    # Declare that all three FI surfaces assert the SAME concept = the margin.
    register_surface_concepts({
        "surface:dashboard.fi_tile": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
        "surface:appendix.fi_table": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
        "surface:fi_verdict": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
    })
    sv = extract_surface_values(g)
    pairs = dict(sv["fi_margin"])
    # Every surface reports the SAME margin value (no divergence).
    assert pairs["surface:dashboard.fi_tile"] == pytest.approx(-148_208.0)
    assert pairs["surface:appendix.fi_table"] == pytest.approx(-148_208.0)
    assert pairs["surface:fi_verdict"] == pytest.approx(-148_208.0)


def test_extract_surface_values_ignores_surfaces_with_no_declared_concept():
    g = _fi_graph(margin_value=-148_208.0)
    register_surface_concepts({})  # nothing declared
    assert extract_surface_values(g) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py -q`
Expected: FAIL — `ImportError: cannot import name 'SurfaceConcept'`

- [ ] **Step 3: Write minimal implementation**

Append to `argosy/quality/surface_rendering.py`. We key the concept value off the surface node's INBOUND derived node (not a regex over rendered text), so the value is exactly the one the surface rendered. `register_surface_concepts` replaces a module-level registry (test-local; the orchestrator passes its own map in Phase 1b/1c hydration).

```python
from dataclasses import dataclass

from argosy.quality.derivation_graph import DerivationGraph, NodeKind


@dataclass(frozen=True)
class SurfaceConcept:
    """A concept a surface asserts, bound to the inbound node carrying its value.

    ``concept`` is the cross-surface coherence key; ``value_input_key`` is the
    inbound node whose (numeric) value the surface rendered for that concept.
    Reading the value off the bound node — not a regex over rendered text — keeps
    the extracted value EXACTLY the one the surface displayed."""

    concept: str
    value_input_key: str


# surface_id -> the concepts it asserts. Module-level so a test (and, later,
# hydration) can declare it; replaced wholesale by register_surface_concepts.
_SURFACE_CONCEPTS: dict[str, list[SurfaceConcept]] = {}


def register_surface_concepts(mapping: dict[str, list[SurfaceConcept]]) -> None:
    """Replace the surface->concepts registry (declarative, like
    coherence.surface_registry.SUBJECT_REGISTRY). Phase 1b/1c hydration supplies
    the real map; tests supply a focused one."""
    global _SURFACE_CONCEPTS
    _SURFACE_CONCEPTS = dict(mapping)


def extract_surface_values(graph: DerivationGraph) -> dict[str, list[tuple[str, float]]]:
    """Build the ``surface_values`` map coherence_gate expects:
    ``{concept: [(surface_id, numeric_value), ...]}`` for every SURFACE node that
    declares a concept. The value is read off the surface's bound inbound node,
    so it is the value that surface actually rendered. Non-numeric values are
    skipped (the coherence gate only compares numbers)."""
    out: dict[str, list[tuple[str, float]]] = {}
    for surface_id, concepts in _SURFACE_CONCEPTS.items():
        try:
            node = graph.get(surface_id)
        except Exception:  # noqa: BLE001 — a declared-but-absent surface is skipped
            continue
        if node.kind is not NodeKind.SURFACE:
            continue
        for c in concepts:
            try:
                val = graph.get(c.value_input_key).value
            except Exception:  # noqa: BLE001
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            out.setdefault(c.concept, []).append((surface_id, float(val)))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/surface_rendering.py tests/test_surface_rendering.py
git commit -m "feat(surfaces): extract surface_values (concept -> [(surface,value)]) from rendered nodes"
```

---

### Task 4: Per-change cheap coherence recheck over the whole rendered artifact

**Files:**
- Modify: `argosy/quality/surface_rendering.py`
- Test: `tests/test_surface_rendering.py`

The spec (Layer-3 step 6) requires a cheap, GLOBAL coherence recheck after every change — the deterministic gate over the whole artifact, NOT the 95-min fleet, NOT the promotion gate. We reuse `coherence_gate.check_cross_surface_coherence`, which expects an object with a `.surface_values` attribute. We feed it a lightweight shim wrapping the extracted map.

- [ ] **Step 1: Write the failing test**

```python
from argosy.quality.surface_rendering import recheck_coherence


def test_recheck_coherence_passes_when_surfaces_agree():
    g = _fi_graph(margin_value=-148_208.0)
    register_surface_concepts({
        "surface:dashboard.fi_tile": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
        "surface:fi_verdict": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
    })
    violations = recheck_coherence(g)
    assert violations == []  # both surfaces read the one node -> agree


def test_recheck_coherence_flags_a_planted_divergence():
    # Two surfaces declared to assert the SAME concept but bound to DIFFERENT
    # nodes with sign-flipped values -> the gate must flag it (the basis-flip the
    # graph design eliminates, here forced to prove the recheck catches it).
    g = _fi_graph(margin_value=-148_208.0)
    g.add_node(Node(key="bad.positive_margin", kind=NodeKind.INPUT, value=118_020.0))
    register_surface_concepts({
        "surface:fi_verdict": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
        "surface:dashboard.fi_tile": [SurfaceConcept("fi_margin", "bad.positive_margin")],
    })
    violations = recheck_coherence(g)
    assert len(violations) == 1
    assert "fi_margin" in violations[0].detail
    assert "SIGN FLIP" in violations[0].detail
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py -q`
Expected: FAIL — `ImportError: cannot import name 'recheck_coherence'`

- [ ] **Step 3: Write minimal implementation**

Append to `argosy/quality/surface_rendering.py`. The shim only needs a `surface_values` attribute — exactly what `check_cross_surface_coherence` reads via `getattr(artifact, "surface_values", None)`.

```python
class _CoherenceView:
    """Minimal artifact shim: the only attribute check_cross_surface_coherence
    reads is ``surface_values``. Avoids constructing a full AssembledArtifact for
    the per-change recheck."""

    def __init__(self, surface_values: dict[str, list[tuple[str, float]]]) -> None:
        self.surface_values = surface_values


def recheck_coherence(graph: DerivationGraph):
    """The per-change CHEAP coherence backstop (spec Layer-3 step 6): run the
    deterministic cross-surface coherence gate over the WHOLE rendered artifact
    (every SURFACE node's declared concepts), returning its GateViolations. This
    is sub-second and runs every change; it is NOT the promotion gate (the full
    promote_gate authority set is the publish blocker, out of scope here)."""
    from argosy.quality.coherence_gate import check_cross_surface_coherence

    surface_values = extract_surface_values(graph)
    return check_cross_surface_coherence(_CoherenceView(surface_values))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/surface_rendering.py tests/test_surface_rendering.py
git commit -m "feat(surfaces): per-change cheap deterministic coherence recheck over rendered surfaces"
```

---

### Task 5: `propagate_and_recheck` — one change → recompute → re-render → recheck

**Files:**
- Modify: `argosy/quality/surface_rendering.py`
- Test: `tests/test_surface_rendering.py`

This is the Layer-3 driver for deterministic surfaces: apply an input change, recompute the blast radius, then run the cheap coherence recheck — returning what re-rendered and the coherence verdict so a caller (and, later, the `propagation_events` trace) can see the ripple. Surfaces OUTSIDE the closure must not re-render.

- [ ] **Step 1: Write the failing test**

```python
from argosy.quality.surface_rendering import propagate_and_recheck


def test_propagate_recomputes_only_the_blast_radius_and_rechecks():
    g = _fi_graph(margin_value=-148_208.0)
    # An INDEPENDENT surface not downstream of the margin must NOT re-render.
    g.add_node(Node(key="indep.value", kind=NodeKind.INPUT, value=42.0))
    g.add_node(make_surface_node(
        key="surface:indep_tile",
        inputs=("indep.value",),
        recipe=lambda i: f"indep {i['indep.value']:.0f}",
        compute_version="indep-v1",
    ))
    g.recompute()
    before_indep = g.get("surface:indep_tile").value
    register_surface_concepts({
        "surface:fi_verdict": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
        "surface:dashboard.fi_tile": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
    })

    result = propagate_and_recheck(g, input_key="retirement.fi_total_capital_nis", value=11_000_000.0)

    # The margin + its three surfaces recomputed; the independent surface did not.
    assert "retirement.fi_margin_signed_nis" in result.recomputed
    assert "surface:fi_verdict" in result.recomputed
    assert "surface:indep_tile" not in result.recomputed
    assert g.get("surface:indep_tile").value == before_indep  # byte-identical
    # Coherence recheck ran and passed (surfaces still agree).
    assert result.coherence_violations == []
    assert "reached" in g.get("surface:fi_verdict").value.lower()


def test_propagate_rejects_a_non_input_target():
    g = _fi_graph(margin_value=-148_208.0)
    with pytest.raises(ValueError):
        # The signed margin is DERIVED — not directly settable (derive-don't-inherit).
        propagate_and_recheck(g, input_key="retirement.fi_margin_signed_nis", value=1.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py -q`
Expected: FAIL — `ImportError: cannot import name 'propagate_and_recheck'`

- [ ] **Step 3: Write minimal implementation**

Append to `argosy/quality/surface_rendering.py`. `set_input` already raises `ValueError` on a non-INPUT target and returns the invalidated set; `recompute` returns the recomputed keys in order.

```python
@dataclass(frozen=True)
class PropagationResult:
    """The visible ripple of one change (spec Layer-5.2): what invalidated, what
    recomputed/re-rendered, and the per-change coherence verdict."""

    trigger_input: str
    invalidated: set[str]
    recomputed: list[str]
    coherence_violations: list


def propagate_and_recheck(graph: DerivationGraph, *, input_key: str, value: Any) -> PropagationResult:
    """Apply one input change, recompute the blast radius (re-rendering every
    stale DERIVED + SURFACE node deterministically), then run the cheap
    whole-artifact coherence recheck. Everything outside the closure is reused
    byte-identical (the engine only recomputes stale nodes). Raises ValueError if
    ``input_key`` is not an INPUT node (a DerivedValue is never hand-set)."""
    invalidated = graph.set_input(input_key, value)  # raises on a non-INPUT target
    recomputed = graph.recompute()
    violations = recheck_coherence(graph)
    return PropagationResult(
        trigger_input=input_key,
        invalidated=invalidated,
        recomputed=recomputed,
        coherence_violations=violations,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py -q`
Expected: PASS (12 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/surface_rendering.py tests/test_surface_rendering.py
git commit -m "feat(surfaces): propagate_and_recheck (recompute blast radius + cheap coherence recheck)"
```

---

### Task 6: Prose surface adapter — span-local edit via the existing surgical editor

**Files:**
- Modify: `argosy/quality/surface_rendering.py`
- Test: `tests/test_surface_rendering.py`

Free-text prose surfaces do NOT re-render from scratch — they reuse `argosy.orchestrator.flows.plan_synthesis.surgical_reconcile.surgically_correct_draft`, which edits ONLY the reader's cited span and is forbidden (`_edit_is_safe`) from introducing a number not in the original or the canonical resolver values. The prose surface node's "recipe" is the driver that hands the editor (a) the bodies, (b) the reader verdict, and (c) the resolver context. We expose a thin adapter so a prose surface goes through the SAME converge-safe editor as today, inheriting its span-local guarantee.

- [ ] **Step 1: Write the failing test**

The editor is injectable (`editor=` stub) so the test is fully deterministic — no LLM/DB. We assert (a) only the cited span changed, (b) the rest of the body is byte-identical, and (c) an edit that tries to introduce a NEW number is rejected (span-local + no-new-numbers).

```python
from argosy.quality.surface_rendering import reconcile_prose_surface


class _Finding:
    def __init__(self, kind, detail, surfaces_cited):
        self.kind = kind
        self.detail = detail
        self.surfaces_cited = surfaces_cited


class _Verdict:
    def __init__(self, findings):
        self.findings = findings


def test_prose_surface_edit_is_span_local():
    bodies = {
        "long": "Intro stays. FI is reached today. Outro also stays.",
        "medium": "",
        "short": "",
    }
    verdict = _Verdict([
        _Finding("contradiction", "FI is not reached on the liquid basis",
                 ["FI is reached today"]),
    ])
    # Stub editor: corrects ONLY the cited span, introduces NO new number.
    result = reconcile_prose_surface(
        bodies=bodies,
        reader_verdict=verdict,
        resolved=None,
        editor=lambda prompt: "FI is not yet reached on the liquid basis",
    )
    corrected = result.corrected_bodies["long"]
    assert "FI is not yet reached on the liquid basis" in corrected
    assert corrected.startswith("Intro stays.")   # before-span byte-identical
    assert corrected.endswith("Outro also stays.")  # after-span byte-identical
    assert len(result.edits) == 1


def test_prose_surface_edit_rejects_a_new_number():
    bodies = {"long": "FI is reached today.", "medium": "", "short": ""}
    verdict = _Verdict([
        _Finding("contradiction", "wrong", ["FI is reached today"]),
    ])
    # The stub tries to inject a fabricated ₪999 — must be rejected (no-new-numbers).
    result = reconcile_prose_surface(
        bodies=bodies,
        reader_verdict=verdict,
        resolved=None,
        editor=lambda prompt: "FI short by 999",
    )
    assert result.corrected_bodies["long"] == "FI is reached today."  # unchanged
    assert result.edits == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py -q`
Expected: FAIL — `ImportError: cannot import name 'reconcile_prose_surface'`

- [ ] **Step 3: Write minimal implementation**

Append to `argosy/quality/surface_rendering.py`. This is a thin, DRY pass-through to the existing, already-tested surgical editor — Phase 1d does NOT reimplement span-local editing.

```python
def reconcile_prose_surface(
    *,
    bodies: dict,
    reader_verdict,
    resolved=None,
    editor: Callable[[str], str] | None = None,
):
    """Reconcile a free-text PROSE surface via the existing converge-safe
    surgical editor. This is the prose-surface render path in spec Layer-3 step
    4: instead of regenerating prose, we edit ONLY the reader's cited span
    (forbidden from introducing new numbers), so the fix converges and cannot
    reshuffle the rest of the document. Delegates to
    surgical_reconcile.surgically_correct_draft — Phase 1d reuses it verbatim
    rather than reimplementing span-local editing. ``editor`` is injectable for
    deterministic tests (no LLM/DB)."""
    from argosy.orchestrator.flows.plan_synthesis.surgical_reconcile import (
        surgically_correct_draft,
    )

    return surgically_correct_draft(
        bodies=bodies,
        reader_verdict=reader_verdict,
        resolved=resolved,
        editor=editor,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py -q`
Expected: PASS (14 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/surface_rendering.py tests/test_surface_rendering.py
git commit -m "feat(surfaces): prose-surface adapter over the converge-safe surgical editor (span-local)"
```

---

### Task 7: Module exports + full surface-rendering suite green

**Files:**
- Modify: `argosy/quality/surface_rendering.py`
- Test: `tests/test_surface_rendering.py`

- [ ] **Step 1: Write the failing test**

```python
def test_public_exports():
    import argosy.quality.surface_rendering as sr
    for name in (
        "SurfaceRenderError", "make_surface_node", "render_fi_verdict_text",
        "build_fi_margin_surfaces", "SurfaceConcept", "register_surface_concepts",
        "extract_surface_values", "recheck_coherence", "PropagationResult",
        "propagate_and_recheck", "reconcile_prose_surface",
    ):
        assert name in sr.__all__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py::test_public_exports -q`
Expected: FAIL — `AttributeError: module ... has no attribute '__all__'`

- [ ] **Step 3: Write minimal implementation**

Append to the bottom of `argosy/quality/surface_rendering.py`:

```python
__all__ = [
    "SurfaceRenderError",
    "make_surface_node",
    "render_fi_verdict_text",
    "build_fi_margin_surfaces",
    "SurfaceConcept",
    "register_surface_concepts",
    "extract_surface_values",
    "recheck_coherence",
    "PropagationResult",
    "propagate_and_recheck",
    "reconcile_prose_surface",
]
```

- [ ] **Step 4: Run the full surface-rendering suite + the engine + coherence suites it builds on**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_surface_rendering.py tests/test_derivation_graph.py -q`
Expected: PASS (surface-rendering ~15 + engine ~17)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/surface_rendering.py tests/test_surface_rendering.py
git commit -m "feat(surfaces): public API exports + full surface-rendering suite green"
```

---

## Self-Review

**Spec coverage (Phase 1d slice — SURFACE rendering from nodes):**

| Spec requirement (scope of this plan) | Task |
| --- | --- |
| Surface = a rendered consumer that consumes derived values and renders text (Layer-1 "Surface" node kind) | Task 1 (`make_surface_node` → `NodeKind.SURFACE`) |
| Pure-render surfaces (dashboard tiles, appendix tables, the FI verdict) re-render deterministically (Layer-3 step 4) | Task 2 (`build_fi_margin_surfaces`: tile + table + verdict) |
| One node → many surfaces, so surfaces can't disagree about the same fact; cross-surface contradiction impossible by construction (Goal; Layer-3 step 4) | Task 2 (`test_one_margin_node_feeds_all_surfaces_identically...`, `test_changing_the_input_updates_all_surfaces_with_no_basis_flip`) |
| FI verdict renders from the ONE signed margin (no basis-flip — the +118,020/−148,208 defect) | Task 2 (`render_fi_verdict_text`, mirrors `render_numbers_for_synth` doctrine) |
| Free-text prose surfaces use the existing converge-safe surgical editor (span-local, forbidden from introducing new numbers) (Layer-3 step 4; Risks "Prose surfaces are not pure functions") | Task 6 (`reconcile_prose_surface` → `surgically_correct_draft`); `test_prose_surface_edit_is_span_local`, `test_prose_surface_edit_rejects_a_new_number` |
| Re-verify: GLOBAL for coherence, cheap (deterministic gate over the whole artifact), NOT the 95-min fleet, NOT the promotion gate (Layer-3 step 6) | Task 3 (`extract_surface_values`) + Task 4 (`recheck_coherence` → `check_cross_surface_coherence`) |
| Everything outside the closure reused byte-identical; recompute scoped, coherence verdict whole-artifact (Layer-3 steps 5–6) | Task 5 (`propagate_and_recheck`; `test_propagate_recomputes_only_the_blast_radius_and_rechecks` asserts the independent surface is byte-identical) |
| A request to set a DerivedValue is rejected ("change the inputs or the recipe") (Layer-2) | Task 5 (`test_propagate_rejects_a_non_input_target` — leans on the engine's `set_input` ValueError) |

**Engine/existing-function reuse (DRY — no reinvention):**
- Engine API consumed as defined by `2026-06-18-derivation-graph-engine.md`: `DerivationGraph`, `Node(key, kind, inputs, recipe, compute_version)`, `NodeKind.{INPUT,DERIVED,SURFACE}`, `add_node`, `get`, `set_input` (returns invalidated `set[str]`, raises `ValueError` on non-INPUT), `recompute` (returns recomputed `list[str]`). No engine code is modified.
- Prose editing reuses `argosy.orchestrator.flows.plan_synthesis.surgical_reconcile.surgically_correct_draft(bodies, reader_verdict, resolved, editor)` verbatim — including its `_edit_is_safe` no-new-numbers guard.
- Coherence recheck reuses `argosy.quality.coherence_gate.check_cross_surface_coherence(artifact)` (reads `artifact.surface_values`) and returns its `argosy.quality.gate_types.GateViolation` list with `GateCheck.CROSS_SURFACE_COHERENCE`.
- The FI verdict text matches the doctrine in `argosy.services.plan_numeric_resolver.render_numbers_for_synth` (REACHED/NOT-reached on the signed margin) so the graph-rendered verdict and the synth-prompt verdict state the same conclusion.

**Explicitly out of scope (correctly deferred, with the consuming phase noted):**
- Hydrating the graph from `plan_numeric_resolver` manifest + `sections_json` and supplying the real `_SURFACE_CONCEPTS` map (Phase 1b/1c) — tests build a focused graph and call `register_surface_concepts` directly.
- SQLAlchemy persistence + the `propagation_events` / `dialogue_turns` Replay trace (Layer-5 infra) — `PropagationResult` is the in-memory shape a later persistence layer serializes.
- The change/adjudication substrate + negotiation ladder (Phase 2).
- The full `promote_gate` promotion authority — Task 4's docstring explicitly states the recheck is NOT the promotion gate.

**Placeholder scan:** none — every code step contains complete, runnable code and every run step has an exact command + expected result.

**Type consistency:** `make_surface_node(*, key, inputs, recipe, compute_version)`, `SurfaceConcept(concept, value_input_key)`, `register_surface_concepts(mapping)`, `extract_surface_values(graph) -> dict[str, list[tuple[str, float]]]`, `recheck_coherence(graph) -> list[GateViolation]`, `PropagationResult(trigger_input, invalidated, recomputed, coherence_violations)`, `propagate_and_recheck(graph, *, input_key, value)`, and `reconcile_prose_surface(*, bodies, reader_verdict, resolved, editor)` are used identically across all tasks and tests.

**Open question (surfaced, not assumed):** the real `_SURFACE_CONCEPTS` registry should ultimately be unified with the existing `argosy.quality.coherence.surface_registry.SUBJECT_REGISTRY` (subject_type → SurfaceSite) rather than introducing a parallel map. This plan keeps a focused `SurfaceConcept` registry for the rendering-from-nodes slice; reconciling the two registries belongs to the Phase 1b/1c hydration plan and is flagged there.
