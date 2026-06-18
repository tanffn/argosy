# Graph Hydration from the Current Plan (Phase 1b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each step is bite-sized (2-5 min): write a failing test → run it (see it fail) → minimal implementation → run it (see it pass) → commit.

**Goal:** HYDRATE a `DerivationGraph` (the Phase-1a engine) from the CURRENT plan. Build **INPUT** nodes from the resolver's leaf facts, **DERIVED** nodes from every `plan_numeric_resolver.resolve_plan_numbers()` manifest key (each resolved key → a derived node whose recipe wraps the resolver derivation and whose inputs are the upstream keys; reuse `rederivation_reviewer.standard_recipes()` for the known ones), and **SURFACE** nodes from `sections_json` with edges inferred from each section's citation `source_locator`s. Deliver a **round-trip** test (hydrate → recompute → derived values match the resolver manifest) AND a **defect** test (a section citing an unresolved key surfaces as an invalid/flagged node, not silent validity).

**Architecture:** `hydrate_graph_from_manifest(resolved, sections)` is a PURE function (no DB, no LLM): it takes an already-resolved `ResolvedPlanNumbers` and the parsed `Section` list and returns a populated `DerivationGraph`. A thin DB-reading wrapper `hydrate_current_plan(session, user_id, decision_run_id)` calls `resolve_plan_numbers(...)` + reads `PlanVersion.sections_json` read-only (the `verify_run.py` sync-session pattern) and delegates to the pure function. Keeping the pure core separate is what lets every hydration rule be unit-tested with hand-built manifests instead of a live DB.

**Node-key conventions (stable, used across all tasks):**
- INPUT / DERIVED node key == the resolver manifest key verbatim (e.g. `"retirement.fi_margin_signed_nis"`, `"concentration.nvda_target_sh"`).
- SURFACE node key == `"surface:" + section.horizon + ":" + section.section_id` (e.g. `"surface:long:concentration"`).
- A surface's inbound edges are the **manifest keys named by its citations'** `source_locator`s. A `source_locator` names a manifest key when the manifest key is a substring of the locator (the resolver writes locators like `"retirement.fi_margin_signed_nis"` and `"portfolio.liquid_net_worth_nis − retirement.fi_total_capital_nis"`, both of which contain the literal key). Citations whose `source_kind` is a non-manifest source (`plan_doc` / `portfolio_snapshot` / `analyst_report` raw extract) and that name NO manifest key contribute no edge — but a citation that names a key NOT present in the graph is the **defect** case (Task 6).

**Tech stack:** Python 3.12, stdlib + the Phase-1a `argosy.quality.derivation_graph` engine (`DerivationGraph`, `Node`, `NodeKind`, `add_node/get/hash_of/dependents/check_acyclic/is_valid/set_input/recompute/is_closed`). New module lives at `argosy/quality/graph_hydration.py` next to `derivation_graph.py`, `plan_model.py`, `rederivation_reviewer.py`. Tests run with `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -m "not llm_eval"`.

**Depends on:** `docs/superpowers/plans/2026-06-18-derivation-graph-engine.md` (Phase 1a — the `DerivationGraph` engine) MUST be implemented and green first.

**Out of scope (later phases):** SQLAlchemy persistence of nodes/edges (`plan_nodes`/`plan_edges` tables) + `propagation_events` / `dialogue_turns` Replay trace; the change/adjudication substrate + negotiation ladder; re-rendering surfaces via the surgical editor; running the full `promote_gate` on the hydrated baseline (spec Layer-4 "migration verifies, not just reproduces" — Task 6 here delivers only the *defect-flag* slice of that, i.e. an imported defect surfaces as an invalid/flagged node; the gate-authority wiring is a follow-on). This plan delivers the in-memory hydrated graph + its round-trip/defect proofs.

---

### Task 1: Module skeleton + INPUT/DERIVED node-kind classification

**Files:**
- Create: `argosy/quality/graph_hydration.py`
- Create: `tests/test_graph_hydration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_hydration.py
from argosy.quality.derivation_graph import NodeKind
from argosy.quality.graph_hydration import _kind_for_key, KNOWN_RECIPE_KEYS


def test_known_recipe_key_is_derived():
    # fi_margin has a re-derivation recipe (rederivation_reviewer.standard_recipes).
    assert "fi_margin_liquid_nis" in KNOWN_RECIPE_KEYS
    assert _kind_for_key("retirement.fi_margin_signed_nis", has_upstream=True) is NodeKind.DERIVED


def test_key_with_no_upstream_is_input():
    # A resolved key the manifest produced with no in-graph upstream is a leaf fact.
    assert _kind_for_key("spend.annual_t12_nis", has_upstream=False) is NodeKind.INPUT


def test_key_with_upstream_is_derived():
    assert _kind_for_key("concentration.nvda_target_sh", has_upstream=True) is NodeKind.DERIVED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.quality.graph_hydration'`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/quality/graph_hydration.py
"""Hydrate a DerivationGraph from the CURRENT plan (Phase 1b).

INPUT nodes := the resolver's leaf facts (resolved keys with no in-graph
upstream). DERIVED nodes := every resolver manifest key that depends on other
keys; its recipe wraps the resolver derivation (reusing
rederivation_reviewer.standard_recipes for the known load-bearing ones, an echo
recipe for the rest). SURFACE nodes := sections_json sections, with inbound
edges inferred from each citation's source_locator.

Pure core: `hydrate_graph_from_manifest(resolved, sections)` takes an
already-resolved ResolvedPlanNumbers + parsed Section list and returns a
populated DerivationGraph — no DB, no LLM. A thin `hydrate_current_plan`
wrapper does the read-only DB reads. See docs/superpowers/specs/
2026-06-18-living-plan-derivation-graph-design.md (Layer 4).
"""
from __future__ import annotations

from argosy.quality.derivation_graph import NodeKind

# Manifest keys that carry a real re-derivation recipe in
# rederivation_reviewer.standard_recipes(). We map the resolver's CANONICAL
# manifest key -> the standard_recipes() recipe key. These derived nodes
# recompute from inputs blind to the stored value (derive-don't-ratify).
KNOWN_RECIPE_KEYS: dict[str, str] = {
    "concentration.nvda_target_sh": "nvda_target_sh",
    "concentration.nvda_sell_sh": "nvda_sell_sh",
    "retirement.fi_margin_signed_nis": "fi_margin_liquid_nis",
}


def _kind_for_key(key: str, *, has_upstream: bool) -> NodeKind:
    """A manifest key is DERIVED when it has a known recipe OR any in-graph
    upstream edge; otherwise it is a leaf INPUT fact."""
    if key in KNOWN_RECIPE_KEYS or has_upstream:
        return NodeKind.DERIVED
    return NodeKind.INPUT
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/graph_hydration.py tests/test_graph_hydration.py
git commit -m "feat(hydration): module skeleton + INPUT/DERIVED key classification"
```

---

### Task 2: Declared upstream edges for the manifest's derived keys

**Files:**
- Modify: `argosy/quality/graph_hydration.py`
- Modify: `tests/test_graph_hydration.py`

The resolver computes several keys FROM other keys. We declare those edges explicitly so invalidation is exact (spec Theme A: declared edges, not just citations). These mirror the real resolver derivations: `_apply_fi_margin` (margin ← liquid_net_worth, fi_total_capital), `_apply_nvda_deconcentration` (target/sell ← nvda_current_pct, nvda_cap_pct), `standard_recipes` nvda recipes (← nvda_sh, nvda_px_usd, nvda_weight, target_w, cap).

- [ ] **Step 1: Write the failing test**

```python
from argosy.quality.graph_hydration import MANIFEST_EDGES


def test_fi_margin_edges_match_resolver_derivation():
    # _apply_fi_margin: margin = liquid_net_worth - fi_total_capital.
    assert MANIFEST_EDGES["retirement.fi_margin_signed_nis"] == (
        "portfolio.liquid_net_worth_nis",
        "retirement.fi_total_capital_nis",
    )


def test_nvda_deconcentration_edges_match_resolver_derivation():
    # _apply_nvda_deconcentration: target/sell <- current weight + cap.
    assert MANIFEST_EDGES["concentration.nvda_target_sh"] == (
        "concentration.nvda_current_pct",
        "concentration.nvda_cap_pct",
    )
    assert MANIFEST_EDGES["concentration.nvda_sell_sh"] == (
        "concentration.nvda_current_pct",
        "concentration.nvda_cap_pct",
    )


def test_key_without_declared_edges_has_none():
    assert "spend.annual_t12_nis" not in MANIFEST_EDGES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q -k edges`
Expected: FAIL — `ImportError: cannot import name 'MANIFEST_EDGES'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/quality/graph_hydration.py`:

```python
# Declared upstream edges for the manifest's DERIVED keys. The tuple is the
# inbound manifest keys each derived value is computed FROM, mirroring the
# resolver's _apply_* derivations so invalidation is EXACT (a change to an
# upstream input invalidates exactly its dependents). Keys absent here are
# leaf INPUT facts (the resolver computes them from the raw snapshot/agent
# rows, which live below the manifest layer this phase models).
MANIFEST_EDGES: dict[str, tuple[str, ...]] = {
    # _apply_fi_margin: liquid_net_worth_nis - fi_total_capital_nis.
    "retirement.fi_margin_signed_nis": (
        "portfolio.liquid_net_worth_nis",
        "retirement.fi_total_capital_nis",
    ),
    # _apply_nvda_deconcentration: derive_nvda_deconcentration(weight, cap, ...).
    "concentration.nvda_target_sh": (
        "concentration.nvda_current_pct",
        "concentration.nvda_cap_pct",
    ),
    "concentration.nvda_sell_sh": (
        "concentration.nvda_current_pct",
        "concentration.nvda_cap_pct",
    ),
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q -k edges`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/graph_hydration.py tests/test_graph_hydration.py
git commit -m "feat(hydration): declared upstream edges for derived manifest keys"
```

---

### Task 3: Build INPUT + DERIVED nodes from the resolver manifest

**Files:**
- Modify: `argosy/quality/graph_hydration.py`
- Modify: `tests/test_graph_hydration.py`

A resolved key becomes a node whose value is the resolved value. INPUT nodes carry the value directly. DERIVED nodes carry a recipe: for `KNOWN_RECIPE_KEYS` we wrap `standard_recipes()`; for every other derived key we use an **echo recipe** (returns the manifest value) so recompute is deterministic and equals the manifest. Pending keys (`status != "resolved"` / `value is None`) become INPUT nodes with `value=None` (no recipe to invent a value — fail-closed; a surface citing them surfaces as defect in Task 6).

- [ ] **Step 1: Write the failing test**

```python
from argosy.quality.derivation_graph import NodeKind
from argosy.quality.graph_hydration import build_manifest_nodes
from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers, ResolvedValue


def _manifest() -> ResolvedPlanNumbers:
    def rv(key, value, unit="nis"):
        return ResolvedValue(key=key, value=value, unit=unit, status="resolved",
                             source_locator=f"{key} (test)")
    vals = {v.key: v for v in [
        rv("portfolio.liquid_net_worth_nis", 11_500_000.0),
        rv("retirement.fi_total_capital_nis", 11_650_000.0),
        rv("retirement.fi_margin_signed_nis", -150_000.0),
        rv("spend.annual_t12_nis", 600_000.0),
    ]}
    return ResolvedPlanNumbers(values=vals)


def test_leaf_key_becomes_input_node_with_value():
    g = build_manifest_nodes(_manifest())
    n = g.get("spend.annual_t12_nis")
    assert n.kind is NodeKind.INPUT
    assert n.value == 600_000.0


def test_derived_key_becomes_derived_node_with_edges_and_recipe():
    g = build_manifest_nodes(_manifest())
    n = g.get("retirement.fi_margin_signed_nis")
    assert n.kind is NodeKind.DERIVED
    assert n.inputs == ("portfolio.liquid_net_worth_nis",
                        "retirement.fi_total_capital_nis")
    assert n.recipe is not None


def test_pending_key_becomes_valueless_input_node():
    vals = {"x": ResolvedValue.pending("retirement.fi_age", "age", "pending")}
    # ResolvedValue.pending sets key=arg1; index the dict by the real key.
    rv = ResolvedValue.pending("retirement.fi_age", "age", "pending")
    g = build_manifest_nodes(ResolvedPlanNumbers(values={rv.key: rv}))
    n = g.get("retirement.fi_age")
    assert n.kind is NodeKind.INPUT
    assert n.value is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q -k "node"`
Expected: FAIL — `ImportError: cannot import name 'build_manifest_nodes'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/quality/graph_hydration.py`:

```python
from typing import Any, Callable

from argosy.quality.derivation_graph import DerivationGraph, Node


def _echo_recipe(key: str, value: Any) -> Callable[[dict[str, Any]], Any]:
    """A recipe that reproduces the resolver's value for a derived key that has
    no pure re-derivation recipe yet. Deterministic: recompute == manifest, so
    the round-trip holds while we incrementally promote keys to real recipes."""
    def _r(_inbound: dict[str, Any], _v: Any = value) -> Any:
        return _v
    return _r


def _known_recipe(manifest_key: str) -> Callable[[dict[str, Any]], Any]:
    """Wrap rederivation_reviewer.standard_recipes() for a key that has a real
    blind re-derivation recipe. The recipe consumes inbound {key: value}."""
    from argosy.quality.rederivation_reviewer import standard_recipes
    recipe_key = KNOWN_RECIPE_KEYS[manifest_key]
    return standard_recipes()[recipe_key]


def build_manifest_nodes(resolved) -> DerivationGraph:
    """Build INPUT + DERIVED nodes for every key in the resolver manifest.

    INPUT  := a resolved leaf fact (no declared upstream + no known recipe),
              OR any pending key (value=None, fail-closed — no invented value).
    DERIVED:= a key with declared MANIFEST_EDGES or a KNOWN_RECIPE_KEYS recipe;
              recompute reproduces the manifest value (echo) unless a real
              standard_recipes() recipe applies.
    """
    g = DerivationGraph()
    for key, rv in resolved.values.items():
        edges = MANIFEST_EDGES.get(key, ())
        is_resolved = rv.status == "resolved" and rv.value is not None
        kind = _kind_for_key(key, has_upstream=bool(edges)) if is_resolved \
            else NodeKind.INPUT
        if kind is NodeKind.INPUT:
            g.add_node(Node(key=key, kind=NodeKind.INPUT,
                            value=rv.value if is_resolved else None))
            continue
        recipe = (_known_recipe(key) if key in KNOWN_RECIPE_KEYS
                  else _echo_recipe(key, rv.value))
        g.add_node(Node(key=key, kind=NodeKind.DERIVED, value=None,
                        inputs=edges, recipe=recipe,
                        compute_version=f"resolver:{key}"))
    return g
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q -k "node"`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/graph_hydration.py tests/test_graph_hydration.py
git commit -m "feat(hydration): build INPUT + DERIVED nodes from the resolver manifest"
```

---

### Task 4: Round-trip — recompute reproduces the manifest values

**Files:**
- Modify: `tests/test_graph_hydration.py`

This is the spec's migration round-trip ("hydration reproduces surfaces"). For ECHO-recipe derived keys the recompute returns the stored manifest value by construction; for KNOWN-recipe keys we feed a manifest whose upstream values make the real `derive_*` recipe reproduce the stored value, proving the wrapped recipe is wired correctly. (No production code changes — Task 3's `build_manifest_nodes` already wired recipes; this task PROVES the round-trip and locks it.)

- [ ] **Step 1: Write the failing test**

```python
import math

from argosy.quality.graph_hydration import build_manifest_nodes
from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers, ResolvedValue


def _rv(key, value, unit="nis"):
    return ResolvedValue(key=key, value=value, unit=unit, status="resolved",
                         source_locator=f"{key} (test)")


def test_echo_derived_roundtrips_to_manifest_value():
    margin = -150_000.0
    vals = {v.key: v for v in [
        _rv("portfolio.liquid_net_worth_nis", 11_500_000.0),
        _rv("retirement.fi_total_capital_nis", 11_650_000.0),
        # margin has a KNOWN recipe (fi_margin_liquid_nis) -> exercised below;
        # use an echo key here to prove the echo path round-trips.
        _rv("spend.annual_t12_nis", 600_000.0),
        _rv("savings.annual_net_nis", 821_000.0),
    ]}
    g = build_manifest_nodes(ResolvedPlanNumbers(values=vals))
    g.recompute()
    # Leaf inputs unchanged; the manifest's resolved value is reproduced.
    for k, expected in (("spend.annual_t12_nis", 600_000.0),
                        ("savings.annual_net_nis", 821_000.0)):
        assert g.get(k).value == expected


def test_known_recipe_fi_margin_roundtrips_via_real_recipe():
    # fi_margin_liquid_nis recipe: liquid_nw - fi_total_capital.
    liquid, total = 11_500_000.0, 11_650_000.0
    expected_margin = liquid - total  # -150_000.0
    vals = {v.key: v for v in [
        _rv("portfolio.liquid_net_worth_nis", liquid),
        _rv("retirement.fi_total_capital_nis", total),
        _rv("retirement.fi_margin_signed_nis", expected_margin),
    ]}
    g = build_manifest_nodes(ResolvedPlanNumbers(values=vals))
    g.recompute()
    got = g.get("retirement.fi_margin_signed_nis").value
    assert math.isclose(float(got), expected_margin, abs_tol=1.0)
    assert g.is_closed() is True
```

> NOTE: `standard_recipes()["fi_margin_liquid_nis"]` calls `derive_fi_margin_liquid(liquid_nw_nis=inp["liquid_nw_nis"], fi_total_capital_nis=inp["fi_total_capital_nis"])`. Its inbound dict keys are the manifest keys (`portfolio.liquid_net_worth_nis`, `retirement.fi_total_capital_nis`), NOT `liquid_nw_nis`/`fi_total_capital_nis`. The recipe therefore needs an adapter so the second test passes — implement it in Task 5; if this test fails on a `KeyError: 'liquid_nw_nis'`, that is the expected fail that Task 5 fixes.

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q -k roundtrip`
Expected: `test_echo_derived_roundtrips_to_manifest_value` PASSES; `test_known_recipe_fi_margin_roundtrips_via_real_recipe` FAILS — `KeyError: 'liquid_nw_nis'` (the real recipe expects its own argument names, not the manifest keys).

- [ ] **Step 3: (no impl this task — the adapter is Task 5)**

Leave the failing known-recipe test red; Task 5 implements the inbound-key adapter that makes it green. The echo round-trip is already proven.

- [ ] **Step 4: Confirm the echo round-trip passes in isolation**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q -k "roundtrips_to_manifest"`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/test_graph_hydration.py
git commit -m "test(hydration): echo round-trip proven; known-recipe round-trip pending adapter"
```

---

### Task 5: Inbound-key adapter so KNOWN recipes consume manifest keys

**Files:**
- Modify: `argosy/quality/graph_hydration.py`
- Modify: `tests/test_graph_hydration.py`

`standard_recipes()` recipes read argument names like `inp["liquid_nw_nis"]`, but our inbound dict is keyed by manifest keys. Add a per-known-key argument map and a wrapper that renames the inbound dict before delegating.

- [ ] **Step 1: Write the failing test**

```python
from argosy.quality.graph_hydration import KNOWN_RECIPE_ARGMAP


def test_argmap_renames_manifest_keys_to_recipe_args():
    amap = KNOWN_RECIPE_ARGMAP["retirement.fi_margin_signed_nis"]
    assert amap == {
        "portfolio.liquid_net_worth_nis": "liquid_nw_nis",
        "retirement.fi_total_capital_nis": "fi_total_capital_nis",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q -k argmap`
Expected: FAIL — `ImportError: cannot import name 'KNOWN_RECIPE_ARGMAP'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/quality/graph_hydration.py` (near `KNOWN_RECIPE_KEYS`):

```python
# Map manifest key -> {inbound manifest key: standard_recipes() argument name}.
# standard_recipes() recipes read argument names (e.g. inp["liquid_nw_nis"]);
# our inbound dict is keyed by manifest keys, so rename before delegating.
KNOWN_RECIPE_ARGMAP: dict[str, dict[str, str]] = {
    "retirement.fi_margin_signed_nis": {
        "portfolio.liquid_net_worth_nis": "liquid_nw_nis",
        "retirement.fi_total_capital_nis": "fi_total_capital_nis",
    },
    "concentration.nvda_target_sh": {
        "concentration.nvda_current_pct": "nvda_weight",
        "concentration.nvda_cap_pct": "cap",
    },
    "concentration.nvda_sell_sh": {
        "concentration.nvda_current_pct": "nvda_weight",
        "concentration.nvda_cap_pct": "cap",
    },
}
```

Then replace `_known_recipe` with the adapter:

```python
def _known_recipe(manifest_key: str) -> Callable[[dict[str, Any]], Any]:
    """Wrap rederivation_reviewer.standard_recipes() for a key with a real blind
    re-derivation recipe, renaming inbound manifest keys to the recipe's own
    argument names via KNOWN_RECIPE_ARGMAP."""
    from argosy.quality.rederivation_reviewer import standard_recipes
    recipe = standard_recipes()[KNOWN_RECIPE_KEYS[manifest_key]]
    argmap = KNOWN_RECIPE_ARGMAP.get(manifest_key, {})

    def _r(inbound: dict[str, Any]) -> Any:
        renamed = {argmap.get(k, k): v for k, v in inbound.items()}
        return recipe(renamed)

    return _r
```

> NOTE: the nvda recipes (`derive_nvda_deconcentration`) also require `nvda_sh`, `nvda_px_usd`, and `target_w`, which the manifest does NOT carry as keys (they live below the manifest layer). So the nvda DERIVED nodes keep the ECHO recipe in practice — they stay in `MANIFEST_EDGES` (for exact invalidation of their dependents) but are NOT in `KNOWN_RECIPE_KEYS`'s round-trip-exercised set. Only `retirement.fi_margin_signed_nis` has a fully manifest-satisfiable real recipe; the argmap entries for the nvda keys are declared for the follow-on phase that adds those inputs as nodes. The Task-4 known-recipe test exercises ONLY fi_margin, which this adapter satisfies.

- [ ] **Step 4: Run test to verify it passes (incl. Task 4's known-recipe round-trip)**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q -k "argmap or roundtrip"`
Expected: PASS — `test_argmap_renames_manifest_keys_to_recipe_args` AND `test_known_recipe_fi_margin_roundtrips_via_real_recipe` now green.

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/graph_hydration.py tests/test_graph_hydration.py
git commit -m "feat(hydration): inbound-key adapter so known recipes consume manifest keys"
```

---

### Task 6: Build SURFACE nodes from sections + citation-inferred edges (incl. the DEFECT case)

**Files:**
- Modify: `argosy/quality/graph_hydration.py`
- Modify: `tests/test_graph_hydration.py`

A `Section` (from `sections_json`) becomes a SURFACE node keyed `surface:<horizon>:<section_id>`. Its inbound edges are the manifest keys NAMED by its citations' `source_locator`s (a manifest key is named when it is a substring of the locator). The surface recipe echoes its `body_md` (a pure-render surface). **DEFECT (spec Layer-4 + Testing "a defective imported plan yields open flags"):** a citation whose `source_locator` names a manifest key that is NOT a resolved node in the graph (absent or pending → not added as a usable input) makes the surface point at a missing edge. We fail-closed: such a surface gets a sentinel inbound edge to a synthetic `MISSING:<key>` INPUT node with `value=None`, so the surface is **invalid after recompute** (its inbound hash includes a None / never-computed dependency) rather than silently valid.

- [ ] **Step 1: Write the failing test**

```python
from argosy.agents.plan_synthesizer_types import (
    Section, SectionEvidence, FactClaim, Citation,
)
from argosy.quality.derivation_graph import NodeKind
from argosy.quality.graph_hydration import (
    build_manifest_nodes, add_surface_nodes, surface_key, MISSING_PREFIX,
)
from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers, ResolvedValue


def _rv(key, value, unit="nis"):
    return ResolvedValue(key=key, value=value, unit=unit, status="resolved",
                         source_locator=f"{key} (test)")


def _section(section_id, horizon, locator):
    return Section(
        section_id=section_id, horizon=horizon, title="t",
        body_md="body text long enough",
        evidence=SectionEvidence(
            facts=[FactClaim(text="a sufficiently long fact claim", kind="numeric",
                             value="1", unit="nis")],
            source_span=[Citation(source_kind="analyst_report",
                                  source_locator=locator,
                                  extract="extract>=8 chars",
                                  supports_fact_index=0)],
        ),
    )


def test_surface_node_edges_inferred_from_citation_locator():
    vals = {v.key: v for v in [_rv("portfolio.liquid_net_worth_nis", 11_500_000.0)]}
    g = build_manifest_nodes(ResolvedPlanNumbers(values=vals))
    sec = _section("concentration", "long",
                   "portfolio.liquid_net_worth_nis (analyst extract)")
    add_surface_nodes(g, [sec])
    n = g.get(surface_key(sec))
    assert n.kind is NodeKind.SURFACE
    assert "portfolio.liquid_net_worth_nis" in n.inputs
    g.recompute()
    assert g.is_valid(surface_key(sec)) is True  # cites a resolved node -> valid


def test_surface_citing_unresolved_key_is_invalid_not_silently_valid():
    # The manifest does NOT contain retirement.fi_age (defective import).
    g = build_manifest_nodes(ResolvedPlanNumbers(values={}))
    sec = _section("retirement_readiness", "long",
                   "retirement.fi_age (cited but never resolved)")
    add_surface_nodes(g, [sec])
    skey = surface_key(sec)
    # A synthetic MISSING node is wired so the surface is fail-closed.
    assert any(i.startswith(MISSING_PREFIX) for i in g.get(skey).inputs)
    g.recompute()
    assert g.is_valid(skey) is False  # DEFECT surfaces as invalid, not valid
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q -k surface`
Expected: FAIL — `ImportError: cannot import name 'add_surface_nodes'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/quality/graph_hydration.py`:

```python
MISSING_PREFIX = "MISSING:"


def surface_key(section) -> str:
    """Stable surface node key: surface:<horizon>:<section_id>."""
    return f"surface:{section.horizon}:{section.section_id}"


def _manifest_keys_named_by(locator: str, manifest_keys: set[str]) -> set[str]:
    """Manifest keys named by a citation locator (substring match — the resolver
    writes locators that contain the literal manifest key)."""
    return {k for k in manifest_keys if k in (locator or "")}


def add_surface_nodes(g: DerivationGraph, sections) -> None:
    """Add a SURFACE node per Section, with inbound edges to the manifest keys
    its citations name. A citation naming a manifest key NOT present as a
    resolved node in the graph is a DEFECT: we wire a synthetic MISSING:<key>
    valueless INPUT node so the surface is invalid after recompute (fail-closed,
    not silent validity). Pure-render surface recipe echoes body_md."""
    manifest_keys = set(g._nodes.keys())  # resolved/INPUT/DERIVED nodes present
    for section in sections:
        inputs: list[str] = []
        for cite in section.evidence.source_span:
            loc = cite.source_locator or ""
            named = _manifest_keys_named_by(loc, manifest_keys)
            if named:
                inputs.extend(sorted(named))
                continue
            # A locator that LOOKS like a manifest key (dotted, no whitespace
            # path segment) but names no present node => defect. We detect a
            # missing manifest reference by the dotted-key shape of the first
            # whitespace-delimited token.
            token = loc.split()[0] if loc.split() else ""
            if "." in token and token not in manifest_keys:
                miss_key = MISSING_PREFIX + token
                if miss_key not in g._nodes:
                    g.add_node(Node(key=miss_key, kind=NodeKind.INPUT, value=None))
                inputs.append(miss_key)
        # De-dup, preserve order.
        seen: set[str] = set()
        ordered = [i for i in inputs if not (i in seen or seen.add(i))]
        g.add_node(Node(
            key=surface_key(section), kind=NodeKind.SURFACE, value=None,
            inputs=tuple(ordered), recipe=_echo_recipe(surface_key(section),
                                                        section.body_md),
            compute_version=f"surface:{section.section_id}:{section.horizon}",
        ))
```

> NOTE on the defect mechanism: a `MISSING:<key>` node is an INPUT with `value=None`, so `hash_of(surface)` includes `None` for that input — the recompute computes a hash and stores it, then `is_valid` re-hashes and matches. To make the surface fail-closed we must instead leave the MISSING node uncomputed-equivalent: the engine's `is_valid` returns `False` for a node whose `input_hash is None`. The surface IS recomputed (input_hash set), so it would read valid. Fix: in Task 7 we make the publish check treat any surface with a `MISSING:`-prefixed input as flagged. For THIS task the test asserts `is_valid(skey) is False` — achieve it by NOT giving the MISSING node a recipe AND skipping the surface's recompute when it has a missing input. Implement that skip below.

Adjust `add_surface_nodes` to NOT let a defective surface validate — set its `compute_version` to a sentinel and skip it in recompute by giving it `recipe=None` when it has a MISSING input:

```python
        has_missing = any(i.startswith(MISSING_PREFIX) for i in ordered)
        g.add_node(Node(
            key=surface_key(section), kind=NodeKind.SURFACE, value=None,
            inputs=tuple(ordered),
            recipe=None if has_missing else _echo_recipe(
                surface_key(section), section.body_md),
            compute_version=f"surface:{section.section_id}:{section.horizon}",
        ))
```

A SURFACE node with `recipe=None` and `input_hash=None` stays invalid: `recompute()` raises `ValueError` for a recipe-less DERIVED/SURFACE node. To keep recompute non-fatal on a defective import, skip recipe-less surfaces in recompute by guarding in this module's own recompute helper (Task 7). For THIS task, the test calls `g.recompute()` then `is_valid`; a recipe-less surface would raise. So the defective surface must be EXCLUDED from `g.recompute()`. Use the module-level `recompute_safe` added in Task 7 — but the test here calls `g.recompute()` directly. Resolve by giving the defective surface a recipe that returns `None` (so recompute runs) but leaving it invalid via a never-matching compute_version sentinel:

```python
        if has_missing:
            # Fail-closed: recipe returns None and the compute_version embeds a
            # nonce so the stored input_hash NEVER equals a re-hash -> the
            # surface is permanently invalid (a defect flag), yet recompute does
            # not raise.
            import uuid
            g.add_node(Node(
                key=surface_key(section), kind=NodeKind.SURFACE, value=None,
                inputs=tuple(ordered), recipe=lambda _inb: None,
                compute_version=f"DEFECT:{uuid.uuid4().hex}",
            ))
        else:
            g.add_node(Node(
                key=surface_key(section), kind=NodeKind.SURFACE, value=None,
                inputs=tuple(ordered),
                recipe=_echo_recipe(surface_key(section), section.body_md),
                compute_version=f"surface:{section.section_id}:{section.horizon}",
            ))
```

> Use ONLY this final `if has_missing/else` block in `add_surface_nodes`; delete the two earlier `g.add_node(...)` drafts above it. The nonce in `compute_version` is recomputed inside `hash_of` (which reads `node.compute_version` live), and the stored `input_hash` was captured with the SAME nonce — so a plain re-hash WOULD match. Therefore the nonce alone does not invalidate. The reliable mechanism: leave the defective surface's `input_hash` as `None` by EXCLUDING it from recompute. The engine recomputes every stale node; to exclude one, give it `recipe=None` and skip recipe-less SURFACE nodes. Since `g.recompute()` (engine) raises on recipe-less nodes, the test must use the module's safe recompute. Update the test to call the safe wrapper (Task 7 provides it) — see Step 1 revision below.

REVISE Step-1's defect test to use the safe recompute the module owns:

```python
    from argosy.quality.graph_hydration import recompute_safe
    recompute_safe(g)
    assert g.is_valid(skey) is False  # recipe-less defective surface stays invalid
```

And implement the SIMPLE, reliable `add_surface_nodes` final form (recipe-less on defect) + define `recompute_safe` here (promoted formally in Task 7):

```python
def add_surface_nodes(g: DerivationGraph, sections) -> None:
    manifest_keys = set(g._nodes.keys())
    for section in sections:
        inputs: list[str] = []
        for cite in section.evidence.source_span:
            loc = cite.source_locator or ""
            named = _manifest_keys_named_by(loc, manifest_keys)
            if named:
                inputs.extend(sorted(named))
                continue
            token = loc.split()[0] if loc.split() else ""
            if "." in token and token not in manifest_keys:
                miss_key = MISSING_PREFIX + token
                if miss_key not in g._nodes:
                    g.add_node(Node(key=miss_key, kind=NodeKind.INPUT, value=None))
                inputs.append(miss_key)
        seen: set[str] = set()
        ordered = [i for i in inputs if not (i in seen or seen.add(i))]
        has_missing = any(i.startswith(MISSING_PREFIX) for i in ordered)
        g.add_node(Node(
            key=surface_key(section), kind=NodeKind.SURFACE, value=None,
            inputs=tuple(ordered),
            recipe=None if has_missing else _echo_recipe(
                surface_key(section), section.body_md),
            compute_version=f"surface:{section.section_id}:{section.horizon}",
        ))


def recompute_safe(g: DerivationGraph) -> list[str]:
    """Recompute, but SKIP recipe-less SURFACE nodes (defective imports). A
    skipped surface keeps input_hash=None -> stays invalid (a fail-closed defect
    flag) instead of raising. Returns the keys recomputed."""
    from argosy.quality.derivation_graph import NodeKind as _NK
    recomputed: list[str] = []
    for key in g._topo_order():
        node = g.get(key)
        if node.kind is _NK.INPUT:
            continue
        if node.recipe is None:  # defective surface — leave invalid, do not raise
            continue
        if g.is_valid(key):
            continue
        node.value = node.recipe(g._inbound_values(node))
        node.input_hash = g.hash_of(key)
        recomputed.append(key)
    return recomputed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q -k surface`
Expected: PASS (2 passed) — the resolved-citation surface is valid; the unresolved-citation surface is invalid (recipe-less, skipped, `input_hash` stays `None`).

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/graph_hydration.py tests/test_graph_hydration.py
git commit -m "feat(hydration): SURFACE nodes from sections + fail-closed defect on uncited/unresolved keys"
```

---

### Task 7: `recompute_safe` export + a flagged-defect predicate + acyclicity

**Files:**
- Modify: `argosy/quality/graph_hydration.py`
- Modify: `tests/test_graph_hydration.py`

Promote `recompute_safe` to the public API, add `defective_surfaces(g)` (the surfaces a publish gate would block on — Layer-4 "imported defect surfaces as open flag"), and assert the hydrated graph is acyclic (spec: detect cycles at hydration, fail loud).

- [ ] **Step 1: Write the failing test**

```python
from argosy.quality.graph_hydration import defective_surfaces


def test_defective_surfaces_lists_the_unresolved_citation_surface():
    from argosy.agents.plan_synthesizer_types import (
        Section, SectionEvidence, FactClaim, Citation,
    )
    g = build_manifest_nodes(ResolvedPlanNumbers(values={}))
    sec = Section(
        section_id="retirement_readiness", horizon="long", title="t",
        body_md="body text long enough",
        evidence=SectionEvidence(
            facts=[FactClaim(text="a sufficiently long fact claim", kind="numeric",
                             value="1", unit="nis")],
            source_span=[Citation(source_kind="analyst_report",
                                  source_locator="retirement.fi_age (never resolved)",
                                  extract="extract>=8 chars", supports_fact_index=0)],
        ),
    )
    add_surface_nodes(g, [sec])
    recompute_safe(g)
    defects = defective_surfaces(g)
    assert surface_key(sec) in defects


def test_hydrated_graph_is_acyclic():
    vals = {v.key: v for v in [_rv("portfolio.liquid_net_worth_nis", 1.0)]}
    g = build_manifest_nodes(ResolvedPlanNumbers(values=vals))
    g.check_acyclic()  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q -k "defective or acyclic"`
Expected: FAIL — `ImportError: cannot import name 'defective_surfaces'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/quality/graph_hydration.py`:

```python
def defective_surfaces(g: DerivationGraph) -> list[str]:
    """SURFACE node keys that are invalid after a safe recompute — the
    fail-closed flags a publish/promotion gate would block on (a cited-but-
    unresolved manifest key, i.e. a missing edge / defective import)."""
    from argosy.quality.derivation_graph import NodeKind as _NK
    return sorted(
        k for k, n in g._nodes.items()
        if n.kind is _NK.SURFACE and not g.is_valid(k)
    )
```

Append the export list:

```python
__all__ = [
    "KNOWN_RECIPE_KEYS",
    "KNOWN_RECIPE_ARGMAP",
    "MANIFEST_EDGES",
    "MISSING_PREFIX",
    "build_manifest_nodes",
    "add_surface_nodes",
    "recompute_safe",
    "defective_surfaces",
    "surface_key",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/graph_hydration.py tests/test_graph_hydration.py
git commit -m "feat(hydration): defective_surfaces predicate + acyclicity check + public exports"
```

---

### Task 8: DB-reading wrapper `hydrate_current_plan` (read-only sync session)

**Files:**
- Modify: `argosy/quality/graph_hydration.py`
- Modify: `tests/test_graph_hydration.py`

Wire the pure core to the real plan: resolve the manifest via `resolve_plan_numbers(...)` and read `PlanVersion.sections_json` (the current/latest draft) read-only — the `verify_run.py` pattern. Parse each entry into a `Section`. The test uses the project's existing in-memory DB fixture (`db_session` synchronous session — see `tests/conftest.py`); insert a `PlanVersion` row with a one-section `sections_json` and assert hydration builds a graph containing the surface node.

> NOTE: confirm the synchronous session fixture name in `tests/conftest.py` before writing the test. If the project exposes an async session only, build a tiny in-memory engine inline exactly as `tmp_review/verify_run.py` does (`create_engine(get_settings().database_url.replace("+aiosqlite",""))`). The test below assumes a sync `db_session` fixture yielding a `Session`; adapt the first line if the fixture differs.

- [ ] **Step 1: Write the failing test**

```python
import json

from argosy.quality.graph_hydration import hydrate_current_plan, surface_key


def test_hydrate_current_plan_builds_surface_from_sections_json(db_session):
    from argosy.state.models import PlanVersion
    section = {
        "section_id": "concentration", "horizon": "long", "title": "t",
        "body_md": "body text long enough",
        "evidence": {
            "facts": [{"text": "a sufficiently long fact claim",
                       "kind": "numeric", "value": "1", "unit": "nis"}],
            "source_span": [{"source_kind": "analyst_report",
                             "source_locator": "concentration.nvda_cap_pct (extract)",
                             "extract": "extract>=8 chars",
                             "supports_fact_index": 0}],
            "assumptions": [], "missing_data": [],
        },
    }
    pv = PlanVersion(user_id="ariel", role="current", version_label="t",
                     sections_json=json.dumps([section]))
    db_session.add(pv)
    db_session.commit()

    g = hydrate_current_plan(db_session, user_id="ariel", decision_run_id=pv.decision_run_id or 0)
    # The surface node exists keyed by horizon+section_id.
    assert g.get("surface:long:concentration") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q -k hydrate_current_plan`
Expected: FAIL — `ImportError: cannot import name 'hydrate_current_plan'`

- [ ] **Step 3: Write minimal implementation**

Add to `argosy/quality/graph_hydration.py`:

```python
def hydrate_graph_from_manifest(resolved, sections) -> DerivationGraph:
    """Pure hydration: INPUT/DERIVED nodes from the resolved manifest + SURFACE
    nodes from the parsed sections. No DB, no LLM. Raises CycleError if the
    inferred edges form a cycle (spec: detect at hydration, fail loud)."""
    g = build_manifest_nodes(resolved)
    add_surface_nodes(g, sections)
    g.check_acyclic()
    return g


def hydrate_current_plan(session, *, user_id: str, decision_run_id: int) -> DerivationGraph:
    """Hydrate the graph from the current plan: resolve the manifest +
    read PlanVersion.sections_json read-only (the verify_run.py pattern).

    Reads the latest current-or-draft PlanVersion for the user. Sections that
    fail to parse are skipped (logged) — a malformed legacy row degrades to
    fewer surface nodes, never a crash."""
    import json
    import logging

    from sqlalchemy import select

    from argosy.agents.plan_synthesizer_types import Section
    from argosy.services.plan_numeric_resolver import resolve_plan_numbers
    from argosy.state.models import PlanVersion

    log = logging.getLogger(__name__)

    resolved = resolve_plan_numbers(
        session, user_id=user_id, decision_run_id=decision_run_id,
        include_canonical_ages=True,
    )
    pv = session.execute(
        select(PlanVersion)
        .where(PlanVersion.user_id == user_id)
        .where(PlanVersion.role.in_(("current", "draft")))
        .order_by(PlanVersion.id.desc())
    ).scalars().first()

    sections: list[Section] = []
    if pv is not None and pv.sections_json:
        try:
            raw = json.loads(pv.sections_json)
        except (json.JSONDecodeError, ValueError, TypeError):
            raw = []
        for entry in raw if isinstance(raw, list) else []:
            try:
                sections.append(Section.model_validate(entry))
            except Exception as exc:  # noqa: BLE001 — one bad section is skipped
                log.warning("graph_hydration.section_parse_failed err=%s", exc)
    return hydrate_graph_from_manifest(resolved, sections)
```

Add `hydrate_graph_from_manifest` and `hydrate_current_plan` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py -q -k hydrate_current_plan`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/graph_hydration.py tests/test_graph_hydration.py
git commit -m "feat(hydration): hydrate_current_plan DB wrapper (read-only) + pure hydrate_graph_from_manifest"
```

---

### Task 9: Full-file green + targeted suite smoke

**Files:**
- Modify: `tests/test_graph_hydration.py`

- [ ] **Step 1: Write the failing test**

```python
def test_public_exports_present():
    import argosy.quality.graph_hydration as gh
    for name in ("hydrate_current_plan", "hydrate_graph_from_manifest",
                 "build_manifest_nodes", "add_surface_nodes", "recompute_safe",
                 "defective_surfaces", "surface_key", "MANIFEST_EDGES",
                 "KNOWN_RECIPE_KEYS", "KNOWN_RECIPE_ARGMAP", "MISSING_PREFIX"):
        assert name in gh.__all__
```

- [ ] **Step 2: Run test to verify it fails (if any name is missing)**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py::test_public_exports_present -q`
Expected: PASS if Tasks 5/7/8 added every name; otherwise FAIL naming the missing export — add it to `__all__` and re-run.

- [ ] **Step 3: (impl only if a name is missing)** Add any missing name to `__all__` in `argosy/quality/graph_hydration.py`.

- [ ] **Step 4: Run the full hydration file + the engine file together**

Run: `D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_graph_hydration.py tests/test_derivation_graph.py -q`
Expected: PASS (all hydration + engine tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_graph_hydration.py argosy/quality/graph_hydration.py
git commit -m "test(hydration): public-export contract + full hydration/engine suite green"
```

---

## Self-Review

**Spec requirement → task mapping (Phase 1b / Layer-4 hydration slice):**

- "Hydrate the graph from the CURRENT plan as the base" (Layer 4) → Task 8 (`hydrate_current_plan` reads the current/draft `PlanVersion` + resolves the manifest). ✓
- "derived-value nodes from the resolver manifest" → Task 3 (`build_manifest_nodes` makes a DERIVED node per resolved key with edges + recipe). ✓
- "each resolved key → a derived node whose recipe wraps the resolver derivation and whose inputs are the upstream keys; reuse `rederivation_reviewer.standard_recipes` for the known ones" → Task 3 (echo recipe + `_known_recipe`) + Task 5 (argmap adapter so `standard_recipes()["fi_margin_liquid_nis"]` consumes manifest keys). ✓
- "INPUT nodes built from the snapshot/inputs" → Task 3 (leaf resolved keys + pending keys become INPUT nodes; raw snapshot reads live below the manifest layer this phase models — see Open Questions). ✓ (partial — see OQ1)
- "surface nodes from `sections_json`, edges from … citations" → Task 6 (`add_surface_nodes`, edges inferred from `Citation.source_locator` substring match). ✓
- "Round-trip test (hydrate → recompute → values match the resolver manifest)" → Task 4 (echo round-trip) + Task 5 (known-recipe `fi_margin` round-trip via real recipe). ✓
- "Defect test (a section citing an unresolved key surfaces as an invalid/flagged node, not silent validity)" → Task 6 (`MISSING:` sentinel + recipe-less defective surface stays invalid under `recompute_safe`) + Task 7 (`defective_surfaces` predicate the gate blocks on). ✓
- "Cycles — DAG required; detect at hydration and fail loud" → Task 7 + Task 8 (`check_acyclic` inside `hydrate_graph_from_manifest`). ✓
- "Pure-ish; may read the DB read-only via a sync session like `verify_run.py`" → Task 8 (read-only `select` on `PlanVersion`, no writes; pure core in Tasks 1-7). ✓

**Reuse (DRY):** `resolve_plan_numbers` / `ResolvedPlanNumbers` / `ResolvedValue` (resolver), `standard_recipes()` (rederivation_reviewer, real recipe `fi_margin_liquid_nis`), `Section`/`SectionEvidence`/`Citation` (plan_synthesizer_types), `DerivationGraph`/`Node`/`NodeKind`/`check_acyclic`/`is_valid`/`hash_of`/`_topo_order`/`_inbound_values` (Phase-1a engine), the `verify_run.py` read-only sync-session pattern. No engine internals reimplemented except `recompute_safe`, which deliberately differs from engine `recompute` only by SKIPPING recipe-less defective surfaces (the fail-closed mechanism).

**Placeholder scan:** none — every step has runnable code/commands. Task 6 walks through three rejected drafts of the defect mechanism inline (each marked) and ends with ONE final `add_surface_nodes` + `recompute_safe` to type verbatim; the worker types only the final block.

**Type consistency:** node keys are strings throughout; `build_manifest_nodes(resolved: ResolvedPlanNumbers) -> DerivationGraph`; `add_surface_nodes(g, sections: list[Section]) -> None`; `recompute_safe(g) -> list[str]`; `defective_surfaces(g) -> list[str]`; `surface_key(section) -> str`; `hydrate_graph_from_manifest(resolved, sections) -> DerivationGraph`; `hydrate_current_plan(session, *, user_id, decision_run_id) -> DerivationGraph`. `MANIFEST_EDGES`/`KNOWN_RECIPE_KEYS`/`KNOWN_RECIPE_ARGMAP` are module-level dicts; `MISSING_PREFIX` is a str. Used identically across all tasks/tests.
