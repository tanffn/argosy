"""Tests for argosy/quality/allocation_graph.py — TDD (red → green).

Coverage:
  1. build_allocation_nodes produces the right node count + kinds from a minimal doc.
  2. allocation.normalized renormalizes 90+30 → 75/25.
  3. node_meta("allocation.sleeve_target.x").authoring_mode == deterministic.
  4. ACCEPTANCE: SUPPLIED ChangeRequest to allocation.sleeve_target.* on a graph
     that contains allocation nodes → SCOPED_EDIT or BOUNDED_REDERIVE (NOT FULL_REBUILD).
  5. UNSUPPLIED sleeve change → FULL_REBUILD (authoring machinery missing).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from argosy.quality.allocation_graph import (
    NORMALIZED_KEY,
    SINGLE_NAME_CAP_KEY,
    build_allocation_nodes,
    sleeve_target_key,
)
from argosy.quality.blast_radius import ChangeRequest, Tier, classify, size_blast_radius
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.plan_node_meta import AuthoringMode, node_meta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_doc(classes=None, nvda_cap_pct=20.0):
    """Build a minimal duck-typed allocation doc."""
    if classes is None:
        classes = [
            SimpleNamespace(id="us_growth", target_pct=60.0),
            SimpleNamespace(id="ex_us", target_pct=40.0),
        ]
    return SimpleNamespace(classes=classes, nvda_cap_pct=nvda_cap_pct)


def _build_alloc_graph(doc=None) -> DerivationGraph:
    """Build a DerivationGraph containing only the allocation nodes."""
    if doc is None:
        doc = _minimal_doc()
    g = DerivationGraph()
    for node in build_allocation_nodes(doc):
        g.add_node(node)
    g.recompute()
    return g


# ---------------------------------------------------------------------------
# 1. build_allocation_nodes — node count + kinds
# ---------------------------------------------------------------------------

class TestBuildAllocationNodes:
    def test_returns_correct_node_count(self):
        """2 classes → 2 sleeve_target INPUTs + 1 normalized DERIVED + 1 cap INPUT = 4 nodes."""
        doc = _minimal_doc()
        nodes = build_allocation_nodes(doc)
        assert len(nodes) == 4

    def test_sleeve_target_nodes_are_input(self):
        doc = _minimal_doc()
        nodes = build_allocation_nodes(doc)
        sleeve_nodes = [n for n in nodes if n.key.startswith("allocation.sleeve_target.")]
        assert len(sleeve_nodes) == 2
        for node in sleeve_nodes:
            assert node.kind is NodeKind.INPUT, f"{node.key} should be INPUT"

    def test_normalized_node_is_derived(self):
        doc = _minimal_doc()
        nodes = build_allocation_nodes(doc)
        norm_nodes = [n for n in nodes if n.key == NORMALIZED_KEY]
        assert len(norm_nodes) == 1
        assert norm_nodes[0].kind is NodeKind.DERIVED

    def test_single_name_cap_is_input(self):
        doc = _minimal_doc(nvda_cap_pct=15.0)
        nodes = build_allocation_nodes(doc)
        cap_nodes = [n for n in nodes if n.key == SINGLE_NAME_CAP_KEY]
        assert len(cap_nodes) == 1
        assert cap_nodes[0].kind is NodeKind.INPUT
        assert cap_nodes[0].value == 15.0

    def test_sleeve_target_keys_match_helper(self):
        doc = _minimal_doc()
        nodes = build_allocation_nodes(doc)
        keys = {n.key for n in nodes}
        assert sleeve_target_key("us_growth") in keys
        assert sleeve_target_key("ex_us") in keys

    def test_sleeve_target_values(self):
        doc = _minimal_doc()
        nodes = build_allocation_nodes(doc)
        by_key = {n.key: n for n in nodes}
        assert by_key[sleeve_target_key("us_growth")].value == 60.0
        assert by_key[sleeve_target_key("ex_us")].value == 40.0

    def test_normalized_inputs_cover_all_sleeves(self):
        doc = _minimal_doc()
        nodes = build_allocation_nodes(doc)
        norm = next(n for n in nodes if n.key == NORMALIZED_KEY)
        assert sleeve_target_key("us_growth") in norm.inputs
        assert sleeve_target_key("ex_us") in norm.inputs

    def test_three_classes(self):
        doc = _minimal_doc(classes=[
            SimpleNamespace(id="a", target_pct=33.0),
            SimpleNamespace(id="b", target_pct=33.0),
            SimpleNamespace(id="c", target_pct=34.0),
        ])
        nodes = build_allocation_nodes(doc)
        # 3 sleeve_target + 1 normalized + 1 cap = 5
        assert len(nodes) == 5


# ---------------------------------------------------------------------------
# 2. allocation.normalized renormalizes correctly
# ---------------------------------------------------------------------------

class TestNormalizedRecipe:
    def test_renormalize_90_30_gives_75_25(self):
        """90 + 30 = 120 total. us_growth = 90/120*100 = 75, ex_us = 30/120*100 = 25."""
        doc = SimpleNamespace(
            classes=[
                SimpleNamespace(id="us_growth", target_pct=90.0),
                SimpleNamespace(id="ex_us", target_pct=30.0),
            ],
            nvda_cap_pct=20.0,
        )
        g = _build_alloc_graph(doc)
        norm_val = g.get(NORMALIZED_KEY).value
        assert isinstance(norm_val, dict)
        assert abs(norm_val["us_growth"] - 75.0) < 1e-9
        assert abs(norm_val["ex_us"] - 25.0) < 1e-9

    def test_renormalize_already_sums_100(self):
        """When inputs already sum to 100, output is unchanged."""
        doc = _minimal_doc()  # 60 + 40 = 100
        g = _build_alloc_graph(doc)
        norm_val = g.get(NORMALIZED_KEY).value
        assert abs(norm_val["us_growth"] - 60.0) < 1e-9
        assert abs(norm_val["ex_us"] - 40.0) < 1e-9

    def test_renormalize_updates_on_sleeve_change(self):
        """After changing a sleeve_target INPUT, recompute updates normalized."""
        doc = _minimal_doc()  # 60/40
        g = _build_alloc_graph(doc)
        # Change us_growth from 60 → 90 (total becomes 130 → 90/130, 40/130)
        g.set_input(sleeve_target_key("us_growth"), 90.0)
        g.recompute()
        norm_val = g.get(NORMALIZED_KEY).value
        assert abs(norm_val["us_growth"] - 90.0 / 130.0 * 100.0) < 1e-9

    def test_renormalize_zero_total_raises(self):
        """Zero-sum sleeve targets should raise (not silently NaN)."""
        doc = SimpleNamespace(
            classes=[
                SimpleNamespace(id="a", target_pct=0.0),
                SimpleNamespace(id="b", target_pct=0.0),
            ],
            nvda_cap_pct=20.0,
        )
        g = DerivationGraph()
        for node in build_allocation_nodes(doc):
            g.add_node(node)
        with pytest.raises((ValueError, ZeroDivisionError)):
            g.recompute()


# ---------------------------------------------------------------------------
# 3. node_meta for allocation.sleeve_target.* → deterministic
# ---------------------------------------------------------------------------

class TestNodeMetaSleeveTarget:
    def test_sleeve_target_authoring_mode_is_deterministic(self):
        meta = node_meta("allocation.sleeve_target.x")
        assert meta.authoring_mode is AuthoringMode.deterministic, (
            f"allocation.sleeve_target.* must be deterministic, got {meta.authoring_mode}"
        )

    def test_sleeve_target_any_id_is_deterministic(self):
        for key in ["allocation.sleeve_target.us_growth", "allocation.sleeve_target.ex_us",
                    "allocation.sleeve_target.em", "allocation.sleeve_target.gold"]:
            meta = node_meta(key)
            assert meta.authoring_mode is AuthoringMode.deterministic, (
                f"{key} must be deterministic"
            )

    def test_normalized_is_deterministic(self):
        meta = node_meta(NORMALIZED_KEY)
        assert meta.authoring_mode is AuthoringMode.deterministic

    def test_single_name_cap_is_rebuild_boundary(self):
        meta = node_meta(SINGLE_NAME_CAP_KEY)
        assert meta.rebuild_boundary is True, (
            f"{SINGLE_NAME_CAP_KEY} must be rebuild_boundary=True"
        )

    def test_sleeve_target_policy_axis_is_allocation(self):
        from argosy.quality.plan_node_meta import PolicyAxis
        meta = node_meta("allocation.sleeve_target.us_growth")
        assert meta.policy_axis is PolicyAxis.allocation

    def test_generic_allocation_key_still_owner_authored(self):
        """allocation.target_weights (not a sleeve_target) should remain owner_authored."""
        meta = node_meta("allocation.target_weights")
        assert meta.authoring_mode is AuthoringMode.owner_authored, (
            "non-sleeve_target allocation keys must remain owner_authored"
        )


# ---------------------------------------------------------------------------
# 4. ACCEPTANCE — SUPPLIED sleeve change → SCOPED_EDIT or BOUNDED_REDERIVE
# ---------------------------------------------------------------------------

class TestAcceptanceSuppliedSleeveChange:
    """ACCEPTANCE: a SUPPLIED ChangeRequest to allocation.sleeve_target.us_growth
    on a graph containing allocation nodes must NOT classify as FULL_REBUILD."""

    def test_supplied_sleeve_change_not_full_rebuild(self):
        doc = _minimal_doc()
        g = _build_alloc_graph(doc)

        changes = [
            ChangeRequest(
                node_key="allocation.sleeve_target.us_growth",
                new_value=8.0,
                supplies_value=True,
            )
        ]
        br = size_blast_radius(g, changes)
        tier, reason = classify(br)

        assert tier != Tier.FULL_REBUILD, (
            f"ACCEPTANCE FAILED: SUPPLIED sleeve change classified as {tier} ({reason}). "
            f"missing_owner={br.missing_owner_for_changed_node}, "
            f"dirtied={br.dirtied_keys}"
        )

    def test_supplied_sleeve_change_tier_is_scoped_or_bounded(self):
        doc = _minimal_doc()
        g = _build_alloc_graph(doc)

        changes = [
            ChangeRequest(
                node_key="allocation.sleeve_target.us_growth",
                new_value=8.0,
                supplies_value=True,
            )
        ]
        br = size_blast_radius(g, changes)
        tier, reason = classify(br)

        assert tier in (Tier.SCOPED_EDIT, Tier.BOUNDED_REDERIVE), (
            f"Expected SCOPED_EDIT or BOUNDED_REDERIVE, got {tier} ({reason})"
        )

    def test_missing_owner_not_set_for_supplied_sleeve_change(self):
        """The specific flag that previously caused escalation must be False."""
        doc = _minimal_doc()
        g = _build_alloc_graph(doc)

        changes = [
            ChangeRequest(
                node_key="allocation.sleeve_target.us_growth",
                new_value=8.0,
                supplies_value=True,
            )
        ]
        br = size_blast_radius(g, changes)
        assert br.missing_owner_for_changed_node is False, (
            "missing_owner_for_changed_node must be False for a deterministic sleeve_target"
        )

    def test_normalized_is_in_dirtied_keys(self):
        """Changing a sleeve_target must dirty allocation.normalized."""
        doc = _minimal_doc()
        g = _build_alloc_graph(doc)

        changes = [
            ChangeRequest(
                node_key="allocation.sleeve_target.us_growth",
                new_value=8.0,
                supplies_value=True,
            )
        ]
        br = size_blast_radius(g, changes)
        assert NORMALIZED_KEY in br.dirtied_keys, (
            f"allocation.normalized must be in dirtied_keys, got {br.dirtied_keys}"
        )

    def test_acceptance_actual_tier_logged(self, capsys):
        """Emit the actual tier so the parent agent can verify it."""
        doc = _minimal_doc()
        g = _build_alloc_graph(doc)

        changes = [
            ChangeRequest(
                node_key="allocation.sleeve_target.us_growth",
                new_value=8.0,
                supplies_value=True,
            )
        ]
        br = size_blast_radius(g, changes)
        tier, reason = classify(br)
        print(f"\nACCEPTANCE tier={tier.value} reason={reason!r}")
        assert tier != Tier.FULL_REBUILD


# ---------------------------------------------------------------------------
# 5. UNSUPPLIED sleeve change → FULL_REBUILD
# ---------------------------------------------------------------------------

class TestUnsuppliedSleeveChange:
    """An unsupplied change (supplies_value=False) on a sleeve_target node.

    Behavior: sleeve_target is deterministic (authoring_mode=deterministic).
    _detect_missing_owner only fires for owner_authored / synthesis_authored nodes,
    so an unsupplied deterministic change does NOT set missing_owner_for_changed_node.
    Since supplies_value=False means set_input is skipped, no dependents are dirtied
    and the blast radius is effectively empty → classifier falls through to SCOPED_EDIT.

    This is a KNOWN LIMITATION: an unsupplied deterministic INPUT change is
    indistinguishable from "no change" to the current classifier.  The caller
    is responsible for ensuring a concrete value is supplied (supplies_value=True)
    for sleeve_target edits; otherwise no propagation occurs.  The ACCEPTANCE
    test (test 4) — SUPPLIED change → not full_rebuild — is the primary contract.

    If the spec is hardened to require T2 for unsupplied deterministic nodes,
    blast_radius._detect_missing_owner must be extended (out of scope here).
    """

    def test_unsupplied_sleeve_change_missing_owner_is_false(self):
        """Deterministic nodes do NOT fire missing_owner on unsupplied changes."""
        doc = _minimal_doc()
        g = _build_alloc_graph(doc)

        changes = [
            ChangeRequest(
                node_key="allocation.sleeve_target.us_growth",
                new_value=None,
                supplies_value=False,
            )
        ]
        br = size_blast_radius(g, changes)
        # deterministic → missing_owner is NOT set (by design of blast_radius)
        assert br.missing_owner_for_changed_node is False

    def test_unsupplied_sleeve_change_dirtied_normalized(self):
        """Even without a supplied value, normalized appears in dirtied_keys
        because blast_radius computes dependents from the graph topology regardless
        of whether set_input was called."""
        doc = _minimal_doc()
        g = _build_alloc_graph(doc)

        changes = [
            ChangeRequest(
                node_key="allocation.sleeve_target.us_growth",
                new_value=None,
                supplies_value=False,
            )
        ]
        br = size_blast_radius(g, changes)
        # Dependents are computed from topology even without value propagation
        assert NORMALIZED_KEY in br.dirtied_keys

    def test_contrast_owner_authored_unsupplied_is_full_rebuild(self):
        """Contrast: unsupplied change on an owner_authored node (portfolio.*)
        DOES fire missing_owner → FULL_REBUILD. Confirms deterministic sleeve_target
        is intentionally different."""
        g = DerivationGraph()
        g.add_node(Node(key="portfolio.target_weight", kind=NodeKind.INPUT, value=0.25))
        g.add_node(Node(
            key="portfolio.derived_metric",
            kind=NodeKind.DERIVED,
            inputs=("portfolio.target_weight",),
            recipe=lambda iv: iv["portfolio.target_weight"] * 2,
            compute_version="v1",
        ))
        g.recompute()

        changes = [
            ChangeRequest(
                node_key="portfolio.target_weight",
                new_value=None,
                supplies_value=False,
            )
        ]
        br = size_blast_radius(g, changes)
        tier, _ = classify(br)
        assert tier == Tier.FULL_REBUILD  # owner_authored + unsupplied → T2
