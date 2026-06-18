"""Canonical live surfaces — two surfaces over the SAME derived node can never
contradict each other (the single-node-render guarantee)."""
from __future__ import annotations

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.live_surfaces import (
    CANONICAL_SUBJECT_NODE,
    EARLIEST_SAFE_AGE_NODE,
    FI_MARGIN_NODE,
    register_canonical_surfaces,
)
from argosy.quality.coherence.surface_registry import SUBJECT_REGISTRY


def _input_node(key: str, value):
    return Node(key=key, kind=NodeKind.INPUT, value=value)


def _build_graph_with_canonical_inputs() -> DerivationGraph:
    """A hermetic graph: the canonical derived nodes are seeded as INPUT nodes
    (so the test can set their value directly) + all canonical surfaces."""
    g = DerivationGraph()
    for node_key in set(CANONICAL_SUBJECT_NODE.values()):
        g.add_node(_input_node(node_key, 0.0))
    register_canonical_surfaces(g)
    g.recompute()
    return g


def test_fi_verdict_and_dashboard_tile_share_basis_and_sign() -> None:
    """The FI verdict and the dashboard FI tile both render from the ONE
    fi_margin node. Changing the margin updates BOTH identically — no basis-flip
    (reached on one, short on the other) is possible."""
    g = _build_graph_with_canonical_inputs()

    # Positive margin -> both must say REACHED.
    g.set_input(FI_MARGIN_NODE, 500_000.0)
    g.recompute()
    verdict = g.get("surface:fi_verdict").value
    tile = g.get("surface:dashboard.fi_tile").value
    assert "REACHED" in verdict
    assert "REACHED" in tile
    assert verdict == tile  # same node, same recipe -> byte-identical

    # Flip the sign -> BOTH flip together to NOT reached. No surface lags.
    g.set_input(FI_MARGIN_NODE, -750_000.0)
    g.recompute()
    verdict2 = g.get("surface:fi_verdict").value
    tile2 = g.get("surface:dashboard.fi_tile").value
    assert "NOT reached" in verdict2
    assert "NOT reached" in tile2
    assert verdict2 == tile2
    # The appendix row reads the SAME magnitude.
    appendix = g.get("surface:appendix.fi_table").value
    assert "750,000" in appendix


def test_retirement_age_headline_and_dashboard_show_identical_age() -> None:
    """The headline age and the dashboard age tile both render the SAME
    earliest_safe_age node -> identical age (kills the 46-vs-dashboard
    divergence)."""
    g = _build_graph_with_canonical_inputs()

    g.set_input(EARLIEST_SAFE_AGE_NODE, 46)
    g.recompute()
    headline = g.get("surface:retirement_age_headline").value
    tile = g.get("surface:dashboard.age_tile").value
    assert "46" in headline
    assert "46" in tile

    # Re-derive to 47 -> BOTH move together; no surface can pin a stale 46.
    g.set_input(EARLIEST_SAFE_AGE_NODE, 47)
    g.recompute()
    assert "47" in g.get("surface:retirement_age_headline").value
    assert "47" in g.get("surface:dashboard.age_tile").value
    assert "46" not in g.get("surface:dashboard.age_tile").value


def test_each_canonicalized_subject_maps_to_exactly_one_node() -> None:
    """Every subject this module claims to canonicalize maps to exactly one node
    key, and (where it overlaps a SUBJECT_REGISTRY subject_type) that registry
    subject is a real one — the unification is one source per subject."""
    # Each registration reports exactly one node key per subject.
    g = _build_graph_with_canonical_inputs()
    regs = register_canonical_surfaces(g)
    by_subject: dict[str, set[str]] = {}
    for r in regs:
        by_subject.setdefault(r.subject_type, set()).add(r.node_key)
    for subject, node_keys in by_subject.items():
        assert len(node_keys) == 1, f"{subject} maps to >1 node: {node_keys}"

    # The mapping itself has exactly one node per subject.
    for subject, node_key in CANONICAL_SUBJECT_NODE.items():
        assert isinstance(node_key, str) and node_key

    # The two distinct net-worth bases map to DIFFERENT nodes (distinct labels).
    assert (
        CANONICAL_SUBJECT_NODE["net_worth_liquid"]
        != CANONICAL_SUBJECT_NODE["net_worth_investable"]
    )

    # Subjects that overlap the coherence SUBJECT_REGISTRY must name a real
    # registry subject_type (unification across the two registries).
    overlapping = set(CANONICAL_SUBJECT_NODE) & set(SUBJECT_REGISTRY)
    assert "fi_capital_sufficiency" in overlapping
    assert "retirement_age_headline" in overlapping
    for subject in overlapping:
        # exactly one canonical node for the shared subject
        assert CANONICAL_SUBJECT_NODE[subject]
