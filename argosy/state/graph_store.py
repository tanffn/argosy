"""Persist / load a DerivationGraph to plan_nodes + plan_edges, emit a
propagation_events row per applied change, and replay a cycle's ripple.

A node's recipe is CODE, not data: save_graph stores the recipe KEY in
provenance_json["recipe_key"]; load_graph re-attaches the callable from a
caller-supplied recipe_registry. No live streaming — propagation_events are
read back on demand by the Replay reader.

See docs/superpowers/specs/2026-06-18-living-plan-derivation-graph-design.md.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.state.models import PlanEdge, PlanNode, PropagationEvent

# recipe key -> recipe(inbound: dict[str, Any]) -> Any
RecipeRegistry = dict[str, Callable[[dict[str, Any]], Any]]


def _recipe_key(node: Node) -> str:
    """The registry key under which a DERIVED/SURFACE node's recipe is found.
    Convention: the node key itself (matches rederivation_reviewer's recipe
    names). Stored in provenance so load can re-attach the callable."""
    return node.key


def save_graph(session: Session, plan_id: int, graph: DerivationGraph) -> None:
    """Replace this plan's persisted nodes + edges with `graph`. Idempotent:
    a re-save of the same graph yields the same rows (delete-then-insert so a
    removed node/edge doesn't linger). The recipe callable is NOT stored — its
    key lands in provenance_json."""
    session.execute(delete(PlanEdge).where(PlanEdge.plan_id == plan_id))
    session.execute(delete(PlanNode).where(PlanNode.plan_id == plan_id))
    session.flush()

    for key in graph.keys():
        node = graph.get(key)
        is_surface = node.kind is NodeKind.SURFACE
        provenance: dict[str, Any] = {}
        if node.kind in (NodeKind.DERIVED, NodeKind.SURFACE):
            provenance["recipe_key"] = _recipe_key(node)
        session.add(PlanNode(
            plan_id=plan_id,
            node_key=key,
            kind=node.kind.value,
            value_json=None if is_surface and not isinstance(node.value, (int, float, list, dict))
            else json.dumps(node.value, default=str),
            content=node.value if is_surface and isinstance(node.value, str) else None,
            input_hash=node.input_hash,
            status_validity="valid" if graph.is_valid(key) else "stale",
            status_flag="none",
            provenance_json=json.dumps(provenance),
            owner="",
            compute_version=node.compute_version,
        ))
        for src in node.inputs:
            session.add(PlanEdge(
                plan_id=plan_id,
                from_node_key=src,
                to_node_key=key,
                edge_kind="named",
            ))
    session.flush()


def _decode_value(row: PlanNode) -> Any:
    if row.kind == NodeKind.SURFACE.value and row.content is not None:
        return row.content
    if row.value_json is None:
        return None
    return json.loads(row.value_json)


def load_graph(
    session: Session,
    plan_id: int,
    recipe_registry: RecipeRegistry | None = None,
) -> DerivationGraph:
    """Rebuild the DerivationGraph for `plan_id` from plan_nodes + plan_edges.
    Inbound edges come from plan_edges (to_node_key == this node). Recipes are
    re-attached from recipe_registry by provenance_json['recipe_key']; a
    DERIVED/SURFACE node whose key is absent loads value-only with recipe=None
    (it is NOT downgraded to an INPUT — that would break invalidation). The
    persisted input_hash is restored so a clean graph loads already-valid."""
    registry = recipe_registry or {}

    node_rows = session.execute(
        select(PlanNode).where(PlanNode.plan_id == plan_id)
    ).scalars().all()
    edge_rows = session.execute(
        select(PlanEdge).where(PlanEdge.plan_id == plan_id)
    ).scalars().all()

    # inputs(to) = sorted [from ...]; sort for deterministic tuple order.
    inputs_by_node: dict[str, list[str]] = {}
    for e in edge_rows:
        inputs_by_node.setdefault(e.to_node_key, []).append(e.from_node_key)

    graph = DerivationGraph()
    for row in node_rows:
        kind = NodeKind(row.kind)
        recipe = None
        if kind in (NodeKind.DERIVED, NodeKind.SURFACE):
            recipe_key = json.loads(row.provenance_json or "{}").get("recipe_key", row.node_key)
            recipe = registry.get(recipe_key)
        graph.add_node(Node(
            key=row.node_key,
            kind=kind,
            value=_decode_value(row),
            inputs=tuple(sorted(inputs_by_node.get(row.node_key, []))),
            recipe=recipe,
            compute_version=row.compute_version,
            input_hash=row.input_hash,
        ))
    return graph


def apply_change(
    session: Session,
    plan_id: int,
    graph: DerivationGraph,
    *,
    cycle_id: str,
    trigger_node_key: str,
    new_value: Any,
    verification_verdicts: dict[str, str] | None = None,
) -> PropagationEvent:
    """Apply an INPUT change to `graph`, propagate (recompute the stale
    closure), persist the updated graph, and record ONE propagation_events row
    whose invalidated / recomputed(old->new) / rerendered sets EXACTLY match
    the engine's closure. Returns the persisted event (flushed, not committed).

    Snapshots old values of the about-to-be-invalidated dependents BEFORE the
    change so old->new is exact."""
    invalidated = graph.dependents(trigger_node_key)
    old_values = {k: graph.get(k).value for k in invalidated}

    graph.set_input(trigger_node_key, new_value)
    recomputed_keys = graph.recompute()  # only the stale closure, in topo order

    recomputed: dict[str, dict[str, Any]] = {}
    rerendered: list[str] = []
    for k in recomputed_keys:
        node = graph.get(k)
        recomputed[k] = {"old": old_values.get(k), "new": node.value}
        if node.kind is NodeKind.SURFACE:
            rerendered.append(k)

    # Persist the now-updated graph so the rows reflect post-propagation state.
    save_graph(session, plan_id, graph)

    event = PropagationEvent(
        plan_id=plan_id,
        cycle_id=cycle_id,
        trigger_node_key=trigger_node_key,
        invalidated_node_keys_json=json.dumps(sorted(invalidated)),
        recomputed_json=json.dumps(recomputed, default=str),
        rerendered_surfaces_json=json.dumps(sorted(rerendered)),
        verification_verdicts_json=json.dumps(verification_verdicts or {}),
    )
    session.add(event)
    session.flush()
    return event


@dataclass
class ReplayStep:
    """One propagation_events row, decoded for the after-the-fact Replay
    reader. The ripple of a single applied change."""
    trigger_node_key: str
    invalidated: list[str]
    recomputed: dict[str, dict[str, Any]]
    rerendered: list[str]
    verdicts: dict[str, str]
    created_at: Any


def replay_cycle(session: Session, plan_id: int, cycle_id: str) -> list[ReplayStep]:
    """Reconstruct, in chronological order, the propagation ripple of every
    applied change in one cycle from its propagation_events rows. Pure read —
    no live streaming. Empty list for an unknown (plan_id, cycle_id)."""
    rows = session.execute(
        select(PropagationEvent)
        .where(PropagationEvent.plan_id == plan_id, PropagationEvent.cycle_id == cycle_id)
        .order_by(PropagationEvent.id)
    ).scalars().all()
    return [
        ReplayStep(
            trigger_node_key=r.trigger_node_key,
            invalidated=json.loads(r.invalidated_node_keys_json),
            recomputed=json.loads(r.recomputed_json),
            rerendered=json.loads(r.rerendered_surfaces_json),
            verdicts=json.loads(r.verification_verdicts_json),
            created_at=r.created_at,
        )
        for r in rows
    ]


__all__ = [
    "RecipeRegistry",
    "save_graph",
    "load_graph",
    "apply_change",
    "replay_cycle",
    "ReplayStep",
]
