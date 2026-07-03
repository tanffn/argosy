"""Allocation sleeve nodes for the derivation graph.

WHY sleeve_target is deterministic-authored INPUT (not DERIVED):
  The coarse ratio-seeds (e.g. growth=13.2%) are supplied by the investment
  policy author, not computed by a formula.  The graph refines AUTHORED target
  values — it does not recompute them from lower-level ratios.  Marking them
  as INPUT / deterministic means a SUPPLIED change lands directly on the node
  without escalating via missing_owner (which only fires for owner_authored /
  synthesis_authored nodes that have no wired agent).

WHY allocation.normalized is DERIVED (not INPUT):
  Given a set of sleeve targets, renormalization is a pure arithmetic recipe.
  Changing one sleeve_target automatically dirtys normalized without needing
  any agent or rebuild.

WHY allocation.single_name_cap is rebuild_boundary:
  Changing the cap is a structural policy change that affects risk constraints
  across the whole plan, not a local sleeve edit.  Its NodeMeta is set to
  rebuild_boundary=True in plan_node_meta._PREFIX_POLICY.
"""
from __future__ import annotations

__all__ = [
    "sleeve_target_key",
    "build_allocation_nodes",
    "NORMALIZED_KEY",
    "SINGLE_NAME_CAP_KEY",
]

from typing import Any


NORMALIZED_KEY = "allocation.normalized"
SINGLE_NAME_CAP_KEY = "allocation.single_name_cap"


def sleeve_target_key(sleeve_id: str) -> str:
    """Return the canonical node key for a sleeve target.

    >>> sleeve_target_key("us_growth")
    'allocation.sleeve_target.us_growth'
    """
    return f"allocation.sleeve_target.{sleeve_id}"


def build_allocation_nodes(doc: Any) -> list:
    """Build derivation-graph Node objects from a TargetAllocationDoc.

    Returns:
      - one ``allocation.sleeve_target.<id>`` INPUT node per class
        (value = target_pct as float).
      - one ``allocation.normalized`` DERIVED node whose recipe renormalizes
        all sleeve_target inputs so their values sum to 100.
      - one ``allocation.single_name_cap`` INPUT node (value = doc.nvda_cap_pct).

    The doc is expected to have:
      - doc.classes: iterable of objects with .label (str) and .target_pct (float)
      - doc.nvda_cap_pct: float  (may be absent → defaults to 20.0)
    """
    from argosy.quality.derivation_graph import Node, NodeKind

    nodes: list[Node] = []
    sleeve_ids: list[str] = []

    for cls in doc.classes:
        if cls.target_pct < 0:
            raise ValueError(
                f"sleeve '{cls.label}' has negative target_pct={cls.target_pct}; "
                f"cannot build a valid allocation node"
            )
        key = sleeve_target_key(cls.label)
        sleeve_ids.append(cls.label)
        nodes.append(Node(
            key=key,
            kind=NodeKind.INPUT,
            value=float(cls.target_pct),
            inputs=(),
            recipe=None,
        ))

    # DERIVED: renormalize all sleeve targets so they sum to 100.
    # Capture a snapshot of sleeve_ids in the closure so that mutations to
    # the outer list after build_allocation_nodes returns cannot corrupt the recipe.
    captured_ids: tuple[str, ...] = tuple(sleeve_ids)
    input_keys: tuple[str, ...] = tuple(sleeve_target_key(sid) for sid in captured_ids)

    def _renormalize(vals: dict[str, Any]) -> dict[str, float]:
        raw = {sid: float(vals[sleeve_target_key(sid)]) for sid in captured_ids}
        total = sum(raw.values())
        if total <= 0:
            raise ValueError(
                f"allocation sleeve target sum is {total}; cannot renormalize to 100"
            )
        return {sid: v / total * 100.0 for sid, v in raw.items()}

    nodes.append(Node(
        key=NORMALIZED_KEY,
        kind=NodeKind.DERIVED,
        value=None,
        inputs=input_keys,
        recipe=_renormalize,
    ))

    # INPUT: single-name cap — changing this is structural (rebuild_boundary in meta).
    cap_pct = getattr(doc, "nvda_cap_pct", None)
    nodes.append(Node(
        key=SINGLE_NAME_CAP_KEY,
        kind=NodeKind.INPUT,
        value=float(cap_pct) if cap_pct is not None else 20.0,
        inputs=(),
        recipe=None,
    ))

    return nodes
