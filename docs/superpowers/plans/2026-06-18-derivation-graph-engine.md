# Derivation-Graph Engine (Phase 1a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure, in-memory derivation-graph engine — nodes + edges, version-stamped hashing, EXACT invalidation, deterministic recompute, cycle detection, runtime expansion — that the living-plan redesign rests on.

**Architecture:** A `DerivationGraph` holds `Node`s keyed by string. INPUT nodes carry authoritative values (incl. *collection* nodes whose value is a list — this is how "all lots / all goals" set-dependencies are modelled: depend on the collection node, so adding a member changes its value and invalidates dependents). DERIVED/SURFACE nodes carry a `recipe(inbound) -> value`. A node's `input_hash` = hash of its inbound node values **plus its `compute_version`** (recipe/template/schema/policy version), so a node goes stale when an input value, a collection's membership, OR the computation itself changes. `recompute()` walks the DAG in topological order and recomputes only stale nodes. No DB, no LLM, no I/O — a deterministic library, unit-tested in isolation.

**Tech Stack:** Python 3.12, stdlib only (`dataclasses`, `enum`, `hashlib`, `json`), pytest. Lives under `argosy/quality/` next to `plan_model.py` and `rederivation_reviewer.py` (the existing derivation-first modules).

**Out of scope for this plan (follow-on plans 1b–1d):** hydrating the graph from `plan_numeric_resolver` / `sections_json`; SQLAlchemy persistence + the `propagation_events` / `dialogue_turns` Replay trace; surface rendering + the surgical editor; the change/adjudication substrate (Phase 2). This plan delivers the engine those build on.

---

### Task 1: Node + NodeKind + graph skeleton

**Files:**
- Create: `argosy/quality/derivation_graph.py`
- Test: `tests/test_derivation_graph.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_derivation_graph.py
import pytest
from argosy.quality.derivation_graph import (
    DerivationGraph, Node, NodeKind, UnknownNodeError,
)


def test_add_and_get_node():
    g = DerivationGraph()
    g.add_node(Node(key="liquid_nw", kind=NodeKind.INPUT, value=11_687_926))
    n = g.get("liquid_nw")
    assert n.kind is NodeKind.INPUT
    assert n.value == 11_687_926


def test_get_unknown_node_raises():
    g = DerivationGraph()
    with pytest.raises(UnknownNodeError):
        g.get("nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'argosy.quality.derivation_graph'`

- [ ] **Step 3: Write minimal implementation**

```python
# argosy/quality/derivation_graph.py
"""Pure in-memory derivation graph: nodes + edges, version-stamped hashing,
exact invalidation, deterministic recompute. No DB / LLM / I/O — the engine the
living-plan redesign rests on. See docs/superpowers/specs/2026-06-18-living-plan-
derivation-graph-design.md.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class NodeKind(str, Enum):
    INPUT = "input"      # authoritative source (incl. collection nodes); never computed
    DERIVED = "derived"  # a number computed from inbound nodes by a recipe
    SURFACE = "surface"  # a rendered consumer computed from inbound nodes by a recipe


class UnknownNodeError(KeyError):
    """Raised when a referenced node key is not in the graph."""


class CycleError(Exception):
    """Raised when the edges form a cycle (the graph must be a DAG)."""


@dataclass
class Node:
    key: str
    kind: NodeKind
    value: Any = None
    # Inbound edges: keys of the nodes this node is derived_from. A collection
    # dependency ("all lots") is just an edge to a collection-valued INPUT node.
    inputs: tuple[str, ...] = ()
    # DERIVED/SURFACE only: (inbound {key: value}) -> value. None for INPUT.
    recipe: Callable[[dict[str, Any]], Any] | None = None
    # Recipe code / render template / schema / policy version. Folded into the
    # hash so a node goes stale when the COMPUTATION changes, inputs unchanged.
    compute_version: str = ""
    # Hash of inbound values + compute_version captured at last successful
    # compute. None until first computed.
    input_hash: str | None = None


class DerivationGraph:
    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}

    def add_node(self, node: Node) -> None:
        self._nodes[node.key] = node

    def get(self, key: str) -> Node:
        try:
            return self._nodes[key]
        except KeyError as exc:
            raise UnknownNodeError(key) from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/derivation_graph.py tests/test_derivation_graph.py
git commit -m "feat(graph): Node/NodeKind + DerivationGraph skeleton"
```

---

### Task 2: Version-stamped input hashing (captures values, membership, compute_version)

**Files:**
- Modify: `argosy/quality/derivation_graph.py`
- Test: `tests/test_derivation_graph.py`

- [ ] **Step 1: Write the failing test**

```python
def test_hash_changes_with_input_value():
    g = DerivationGraph()
    g.add_node(Node(key="a", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="b", kind=NodeKind.INPUT, value=2))
    g.add_node(Node(key="sum", kind=NodeKind.DERIVED, inputs=("a", "b")))
    h1 = g.hash_of("sum")
    g.get("a").value = 99
    assert g.hash_of("sum") != h1


def test_hash_changes_with_collection_membership():
    g = DerivationGraph()
    g.add_node(Node(key="lots", kind=NodeKind.INPUT, value=[1, 2, 3]))
    g.add_node(Node(key="total", kind=NodeKind.DERIVED, inputs=("lots",)))
    h1 = g.hash_of("total")
    g.get("lots").value = [1, 2, 3, 4]  # a new lot joined the collection
    assert g.hash_of("total") != h1


def test_hash_changes_with_compute_version():
    g = DerivationGraph()
    g.add_node(Node(key="a", kind=NodeKind.INPUT, value=1))
    n = Node(key="d", kind=NodeKind.DERIVED, inputs=("a",), compute_version="v1")
    g.add_node(n)
    h1 = g.hash_of("d")
    n.compute_version = "v2"  # the recipe changed; inputs did not
    assert g.hash_of("d") != h1


def test_hash_is_stable_and_order_independent():
    g = DerivationGraph()
    g.add_node(Node(key="a", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="b", kind=NodeKind.INPUT, value=2))
    g.add_node(Node(key="d1", kind=NodeKind.DERIVED, inputs=("a", "b")))
    g.add_node(Node(key="d2", kind=NodeKind.DERIVED, inputs=("b", "a")))
    assert g.hash_of("d1") == g.hash_of("d2")  # input order must not matter
    assert g.hash_of("d1") == g.hash_of("d1")  # stable across calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py -q`
Expected: FAIL — `AttributeError: 'DerivationGraph' object has no attribute 'hash_of'`

- [ ] **Step 3: Write minimal implementation**

Add to `DerivationGraph`:

```python
    def _inbound_values(self, node: Node) -> dict[str, Any]:
        return {k: self.get(k).value for k in node.inputs}

    def hash_of(self, key: str) -> str:
        """Hash of a node's inbound values (sorted, membership-sensitive) + its
        compute_version. Independent of input declaration order; stable across
        calls. This is what `input_hash` is compared against for validity."""
        node = self.get(key)
        payload = {
            "inbound": {
                k: self.get(k).value for k in sorted(node.inputs)
            },
            "compute_version": node.compute_version,
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/derivation_graph.py tests/test_derivation_graph.py
git commit -m "feat(graph): version-stamped inbound hashing (values + membership + compute_version)"
```

---

### Task 3: Transitive dependents + cycle detection

**Files:**
- Modify: `argosy/quality/derivation_graph.py`
- Test: `tests/test_derivation_graph.py`

- [ ] **Step 1: Write the failing test**

```python
from argosy.quality.derivation_graph import CycleError


def test_transitive_dependents():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",)))
    g.add_node(Node(key="z", kind=NodeKind.DERIVED, inputs=("y",)))
    g.add_node(Node(key="other", kind=NodeKind.INPUT, value=9))
    assert g.dependents("x") == {"y", "z"}
    assert g.dependents("y") == {"z"}
    assert g.dependents("other") == set()


def test_cycle_is_detected():
    g = DerivationGraph()
    g.add_node(Node(key="a", kind=NodeKind.DERIVED, inputs=("b",)))
    g.add_node(Node(key="b", kind=NodeKind.DERIVED, inputs=("a",)))
    with pytest.raises(CycleError):
        g.check_acyclic()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'dependents'`

- [ ] **Step 3: Write minimal implementation**

Add to `DerivationGraph`:

```python
    def _direct_dependents(self, key: str) -> set[str]:
        return {n.key for n in self._nodes.values() if key in n.inputs}

    def dependents(self, key: str) -> set[str]:
        """All nodes that depend on `key`, transitively (excludes `key` itself)."""
        seen: set[str] = set()
        stack = list(self._direct_dependents(key))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self._direct_dependents(cur))
        return seen

    def check_acyclic(self) -> None:
        """Raise CycleError if the inputs edges contain a cycle (must be a DAG)."""
        WHITE, GREY, BLACK = 0, 1, 2
        color = {k: WHITE for k in self._nodes}

        def visit(k: str) -> None:
            color[k] = GREY
            for dep in self.get(k).inputs:
                if dep not in color:
                    continue  # forward ref to a not-yet-added node; ignored here
                if color[dep] == GREY:
                    raise CycleError(f"cycle through {k} -> {dep}")
                if color[dep] == WHITE:
                    visit(dep)
            color[k] = BLACK

        for k in list(self._nodes):
            if color[k] == WHITE:
                visit(k)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/derivation_graph.py tests/test_derivation_graph.py
git commit -m "feat(graph): transitive dependents + DAG cycle detection"
```

---

### Task 4: Validity + exact invalidation on input change

**Files:**
- Modify: `argosy/quality/derivation_graph.py`
- Test: `tests/test_derivation_graph.py`

- [ ] **Step 1: Write the failing test**

```python
def test_input_node_is_always_valid():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    assert g.is_valid("x") is True


def test_uncomputed_derived_is_invalid():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",), recipe=lambda i: i["x"] + 1))
    assert g.is_valid("y") is False  # input_hash is None until computed


def test_set_input_rejects_non_input():
    g = DerivationGraph()
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=()))
    with pytest.raises(ValueError):
        g.set_input("y", 5)


def test_set_input_invalidates_exactly_the_dependents():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",), recipe=lambda i: i["x"] + 1))
    g.add_node(Node(key="z", kind=NodeKind.DERIVED, inputs=("y",), recipe=lambda i: i["y"] * 2))
    g.add_node(Node(key="indep", kind=NodeKind.INPUT, value=5))
    g.add_node(Node(key="w", kind=NodeKind.DERIVED, inputs=("indep",), recipe=lambda i: i["indep"]))
    g.recompute()
    assert all(g.is_valid(k) for k in ("y", "z", "w"))
    invalidated = g.set_input("x", 100)
    assert invalidated == {"y", "z"}      # exactly x's dependents
    assert g.is_valid("w") is True        # untouched — not downstream of x
    assert g.is_valid("y") is False
    assert g.is_valid("z") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'is_valid'` (and `set_input`/`recompute` land in Task 5; this test references `recompute` so it will error until Task 5 — see note)

> NOTE: `test_set_input_invalidates_exactly_the_dependents` calls `recompute()`, implemented in Task 5. Implement `is_valid` + `set_input` now; that test will pass once Task 5 lands. The other three validity tests pass in this task. Run the full file after Task 5.

- [ ] **Step 3: Write minimal implementation**

Add to `DerivationGraph`:

```python
    def is_valid(self, key: str) -> bool:
        """INPUT nodes are authoritative → always valid. A DERIVED/SURFACE node
        is valid iff its stored input_hash matches the current inbound hash."""
        node = self.get(key)
        if node.kind is NodeKind.INPUT:
            return True
        if node.input_hash is None:
            return False
        return node.input_hash == self.hash_of(key)

    def set_input(self, key: str, value: Any) -> set[str]:
        """Update an INPUT node's value and return the set of nodes invalidated
        (its transitive dependents). Raises ValueError on a non-INPUT node —
        derived values are never hand-set (derive-don't-inherit)."""
        node = self.get(key)
        if node.kind is not NodeKind.INPUT:
            raise ValueError(f"{key} is {node.kind}, not an INPUT; change inputs/recipe instead")
        node.value = value
        return self.dependents(key)
```

- [ ] **Step 4: Run the validity tests (defer the invalidation test to Task 5)**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py -q -k "valid or set_input_rejects"`
Expected: PASS for `test_input_node_is_always_valid`, `test_uncomputed_derived_is_invalid`, `test_set_input_rejects_non_input`

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/derivation_graph.py tests/test_derivation_graph.py
git commit -m "feat(graph): validity (hash match) + set_input invalidation of exact dependents"
```

---

### Task 5: Deterministic topological recompute

**Files:**
- Modify: `argosy/quality/derivation_graph.py`
- Test: `tests/test_derivation_graph.py`

- [ ] **Step 1: Write the failing test**

```python
def test_recompute_computes_in_dependency_order():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=10))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",), recipe=lambda i: i["x"] + 1))
    g.add_node(Node(key="z", kind=NodeKind.DERIVED, inputs=("y",), recipe=lambda i: i["y"] * 2))
    recomputed = g.recompute()
    assert g.get("y").value == 11
    assert g.get("z").value == 22
    assert recomputed.index("y") < recomputed.index("z")  # y before z
    assert all(g.is_valid(k) for k in ("x", "y", "z"))


def test_recompute_only_touches_stale_nodes():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",), recipe=lambda i: i["x"] + 1))
    g.add_node(Node(key="indep", kind=NodeKind.INPUT, value=5))
    g.add_node(Node(key="w", kind=NodeKind.DERIVED, inputs=("indep",), recipe=lambda i: i["indep"]))
    g.recompute()                      # everything valid
    g.set_input("x", 100)              # only y stale now
    recomputed = g.recompute()
    assert recomputed == ["y"]         # w NOT recomputed (independent)
    assert g.get("y").value == 101


def test_recompute_is_deterministic():
    def build():
        g = DerivationGraph()
        g.add_node(Node(key="a", kind=NodeKind.INPUT, value=3))
        g.add_node(Node(key="b", kind=NodeKind.INPUT, value=4))
        g.add_node(Node(key="s", kind=NodeKind.DERIVED, inputs=("a", "b"),
                        recipe=lambda i: i["a"] + i["b"]))
        g.recompute()
        return g.get("s").value
    assert build() == build() == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py -q -k recompute`
Expected: FAIL — `AttributeError: ... has no attribute 'recompute'`

- [ ] **Step 3: Write minimal implementation**

Add to `DerivationGraph`:

```python
    def _topo_order(self) -> list[str]:
        """Kahn topological order over the inputs edges (raises CycleError)."""
        self.check_acyclic()
        indeg = {k: 0 for k in self._nodes}
        for n in self._nodes.values():
            for dep in n.inputs:
                if dep in self._nodes:
                    indeg[n.key] += 1
        # Sort the ready set by key for STABLE, deterministic ordering.
        ready = sorted(k for k, d in indeg.items() if d == 0)
        order: list[str] = []
        while ready:
            k = ready.pop(0)
            order.append(k)
            for m in sorted(self._direct_dependents(k)):
                indeg[m] -= 1
                if indeg[m] == 0:
                    ready.append(m)
            ready.sort()
        return order

    def recompute(self) -> list[str]:
        """Recompute every stale DERIVED/SURFACE node in dependency order. Returns
        the keys recomputed, in order. Valid nodes are skipped (reused)."""
        recomputed: list[str] = []
        for key in self._topo_order():
            node = self.get(key)
            if node.kind is NodeKind.INPUT:
                continue
            if self.is_valid(key):
                continue
            if node.recipe is None:
                raise ValueError(f"{key} is {node.kind} but has no recipe")
            node.value = node.recipe(self._inbound_values(node))
            node.input_hash = self.hash_of(key)
            recomputed.append(key)
        return recomputed

    def is_closed(self) -> bool:
        """The graph is closed when every node is valid (no stale node)."""
        return all(self.is_valid(k) for k in self._nodes)
```

- [ ] **Step 4: Run the FULL test file (Task 4's invalidation test now passes too)**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py -q`
Expected: PASS (all tests, incl. `test_set_input_invalidates_exactly_the_dependents`)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/derivation_graph.py tests/test_derivation_graph.py
git commit -m "feat(graph): deterministic topological recompute (only stale nodes) + is_closed"
```

---

### Task 6: Graph expansion (add a node at runtime; new + dependents recompute)

**Files:**
- Modify: `argosy/quality/derivation_graph.py`
- Test: `tests/test_derivation_graph.py`

- [ ] **Step 1: Write the failing test**

```python
def test_adding_a_node_at_runtime_expands_and_recomputes():
    # A new holding arrives: add its value node + grow the collection it belongs
    # to. Its surface row is new+invalid; the collection's dependents go stale.
    g = DerivationGraph()
    g.add_node(Node(key="holdings", kind=NodeKind.INPUT, value=["NVDA", "AMD"]))
    g.add_node(Node(key="count", kind=NodeKind.DERIVED, inputs=("holdings",),
                    recipe=lambda i: len(i["holdings"])))
    g.recompute()
    assert g.get("count").value == 2

    # Expansion: a new holding joins.
    g.add_node(Node(key="row:GOOG", kind=NodeKind.SURFACE, inputs=("holdings",),
                    recipe=lambda i: "GOOG in book" if "GOOG" in i["holdings"] else "absent"))
    invalidated = g.set_input("holdings", ["NVDA", "AMD", "GOOG"])
    assert "count" in invalidated
    recomputed = g.recompute()
    assert g.get("count").value == 3          # membership change propagated
    assert g.get("row:GOOG").value == "GOOG in book"
    assert "row:GOOG" in recomputed and "count" in recomputed


def test_is_closed_reflects_pending_new_node():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",), recipe=lambda i: i["x"]))
    g.recompute()
    assert g.is_closed() is True
    g.add_node(Node(key="z", kind=NodeKind.DERIVED, inputs=("x",), recipe=lambda i: i["x"] * 3))
    assert g.is_closed() is False             # new node not yet computed
    g.recompute()
    assert g.is_closed() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py -q -k "expand or pending_new"`
Expected: FAIL — `row:GOOG`/`z` not computed as expected (currently `add_node` + `recompute` already exist, so confirm the assertion that fails is the value/closed check; if it unexpectedly passes, the engine already supports expansion and you commit as-is).

> NOTE: Tasks 1–5 already give `add_node` + membership-sensitive hashing + recompute, so expansion may already work. This task's job is to PROVE it with explicit tests and lock the behavior. If both tests pass with no code change, that is the expected outcome — proceed to commit.

- [ ] **Step 3: Write minimal implementation (only if a test fails)**

If `test_is_closed_reflects_pending_new_node` fails because a freshly-added node is considered valid, confirm `is_valid` returns False when `input_hash is None` (Task 4). No new code should be required; if it is, the fix belongs in `is_valid`/`recompute`, not a new method.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py -q`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/derivation_graph.py tests/test_derivation_graph.py
git commit -m "test(graph): lock runtime expansion (new node + collection growth propagate)"
```

---

### Task 7: Module exports + full-suite smoke

**Files:**
- Modify: `argosy/quality/derivation_graph.py`
- Test: `tests/test_derivation_graph.py`

- [ ] **Step 1: Write the failing test**

```python
def test_public_exports():
    import argosy.quality.derivation_graph as dg
    for name in ("DerivationGraph", "Node", "NodeKind",
                 "UnknownNodeError", "CycleError"):
        assert name in dg.__all__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py::test_public_exports -q`
Expected: FAIL — `AttributeError: module ... has no attribute '__all__'`

- [ ] **Step 3: Write minimal implementation**

Append to the bottom of `argosy/quality/derivation_graph.py`:

```python
__all__ = [
    "DerivationGraph",
    "Node",
    "NodeKind",
    "UnknownNodeError",
    "CycleError",
]
```

- [ ] **Step 4: Run the full engine suite**

Run: `.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_derivation_graph.py -q`
Expected: PASS (all tests, ~17)

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/derivation_graph.py tests/test_derivation_graph.py
git commit -m "feat(graph): public API exports + full engine suite green"
```

---

## Self-Review

**Spec coverage (Phase 1a slice):**
- Node kinds (INPUT/DERIVED/SURFACE) → Task 1. ✓
- Hybrid edges via collection nodes (set/membership dependency) → Task 2 (`test_hash_changes_with_collection_membership`), Task 6. ✓
- Compute-provenance in the hash (codex Theme A) → Task 2 (`test_hash_changes_with_compute_version`). ✓
- Exact invalidation (change X ⇒ exactly X's transitive dependents) → Task 4 (`test_set_input_invalidates_exactly_the_dependents`). ✓
- Deterministic recompute, only stale nodes → Task 5. ✓
- Derived values never hand-set (derive-don't-inherit) → Task 4 (`test_set_input_rejects_non_input`). ✓
- Cycle detection (DAG required) → Task 3. ✓
- Graph expansion on structural change → Task 6. ✓
- **Deferred to plans 1b–1d (correctly out of scope):** resolver/sections hydration; DB persistence + `propagation_events`/`dialogue_turns` Replay trace; surface rendering + surgical editor; the adjudication substrate + negotiation ladder; the promotion gate wiring (already shipped separately). The engine here is what they consume.

**Placeholder scan:** none — every step has runnable code/commands.

**Type consistency:** `Node(key, kind, value, inputs, recipe, compute_version, input_hash)`, `NodeKind.{INPUT,DERIVED,SURFACE}`, and methods `add_node/get/hash_of/dependents/check_acyclic/is_valid/set_input/recompute/is_closed/_topo_order/_direct_dependents/_inbound_values` are used identically across all tasks. `set_input` returns `set[str]`; `recompute` returns `list[str]`; `dependents` returns `set[str]` — consistent in every test.
