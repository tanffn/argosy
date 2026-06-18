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

    def keys(self) -> list[str]:
        """All node keys in insertion order. Lets persistence/hydration enumerate
        the graph without reaching into the private ``_nodes`` dict."""
        return list(self._nodes)

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
        # Eagerly invalidate the transitive dependents: a direct dependent's
        # inbound hash changes immediately (its input value moved), but a deeper
        # dependent reads an intermediate's *value*, which has not been
        # recomputed yet — so its hash would still match and it would falsely
        # read as valid until recompute. Clear input_hash so the whole subtree
        # is stale right now (derive-don't-inherit).
        invalidated = self.dependents(key)
        for dep in invalidated:
            self.get(dep).input_hash = None
        return invalidated

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


__all__ = [
    "DerivationGraph",
    "Node",
    "NodeKind",
    "UnknownNodeError",
    "CycleError",
]
