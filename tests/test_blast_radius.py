"""Tests for argosy.quality.blast_radius — blast-radius sizer + tier classifier.

TDD: tests written BEFORE implementation (red → green).

Coverage:
  * classify() — one test per T2 trigger, per T1 trigger, T0 fallthrough.
    Each asserts BOTH the tier and that reason contains the trigger keyword.
  * size_blast_radius() — integration tests using small real DerivationGraph
    objects; assertions on derived BlastRadius fields.
  * No-mutation guarantee: original graph is NOT changed after sizing.
"""
from __future__ import annotations

import pytest

from argosy.quality.blast_radius import (
    BlastRadius,
    ChangeRequest,
    HardVerdictFlip,
    Tier,
    TierConfig,
    classify,
    size_blast_radius,
)
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind


# ---------------------------------------------------------------------------
# Helpers — build minimal graphs for integration tests
# ---------------------------------------------------------------------------

def _scalar_graph() -> DerivationGraph:
    """Eight nodes: four inputs, four derived.

    Changed node: savings.monthly  → dirtied: savings.total, savings.net (2/8 = 0.25 < 0.34).
    Unrelated inputs (savings.annual, savings.bonus, savings.rsu) are untouched,
    keeping the blast-radius fraction below the T1 threshold so classify → T0.

    All savings.* keys: deterministic, hard_verdict_severity=None, one owner (equity_comp).
    """
    g = DerivationGraph()
    g.add_node(Node(key="savings.monthly", kind=NodeKind.INPUT, value=5_000.0))
    g.add_node(Node(key="savings.annual", kind=NodeKind.INPUT, value=60_000.0))
    g.add_node(Node(key="savings.bonus", kind=NodeKind.INPUT, value=10_000.0))
    g.add_node(Node(key="savings.rsu", kind=NodeKind.INPUT, value=20_000.0))
    g.add_node(Node(
        key="savings.total",
        kind=NodeKind.DERIVED,
        inputs=("savings.monthly",),
        recipe=lambda iv: iv["savings.monthly"] * 12,
        compute_version="v1",
    ))
    g.add_node(Node(
        key="savings.net",
        kind=NodeKind.DERIVED,
        inputs=("savings.total", "savings.annual"),
        recipe=lambda iv: iv["savings.total"] - iv["savings.annual"],
        compute_version="v1",
    ))
    g.add_node(Node(
        key="savings.total_income",
        kind=NodeKind.DERIVED,
        inputs=("savings.bonus", "savings.rsu"),
        recipe=lambda iv: iv["savings.bonus"] + iv["savings.rsu"],
        compute_version="v1",
    ))
    g.add_node(Node(
        key="savings.grand_total",
        kind=NodeKind.DERIVED,
        inputs=("savings.total_income", "savings.annual"),
        recipe=lambda iv: iv["savings.total_income"] + iv["savings.annual"],
        compute_version="v1",
    ))
    g.recompute()
    return g


def _two_owner_graph() -> DerivationGraph:
    """Two inputs from different owner domains (savings.* / fx.*).

    savings.* → equity_comp owner (deterministic)
    fx.rate   → fx owner (deterministic)
    Both have hard_verdict_severity that is not plan_basis (fx=localized,
    savings=None), so a change spanning them routes to T1 via multi-owner,
    not T2 via plan-basis flip.
    """
    g = DerivationGraph()
    g.add_node(Node(key="savings.monthly", kind=NodeKind.INPUT, value=10.0))
    g.add_node(Node(key="fx.rate", kind=NodeKind.INPUT, value=3.7))
    g.add_node(Node(
        key="savings.total",
        kind=NodeKind.DERIVED,
        inputs=("savings.monthly",),
        recipe=lambda iv: iv["savings.monthly"] * 12,
        compute_version="v1",
    ))
    g.recompute()
    return g


def _identity_graph() -> DerivationGraph:
    """Graph containing a plan-identity-axis key."""
    g = DerivationGraph()
    g.add_node(Node(key="retirement.risk_posture", kind=NodeKind.INPUT, value="moderate"))
    g.add_node(Node(key="spend.rate", kind=NodeKind.INPUT, value=50_000.0))
    g.add_node(Node(
        key="spend.total",
        kind=NodeKind.DERIVED,
        inputs=("spend.rate",),
        recipe=lambda iv: iv["spend.rate"] * 1.1,
        compute_version="v1",
    ))
    g.recompute()
    return g


def _owner_authored_graph() -> DerivationGraph:
    """Graph with an owner_authored node (portfolio.* → allocation owner)."""
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
    return g


# ---------------------------------------------------------------------------
# classify() — table-driven: one test per T2 trigger
# ---------------------------------------------------------------------------

class TestClassifyT2:
    """Each T2 trigger in precedence order."""

    def _br(self, **overrides) -> BlastRadius:
        defaults = dict(
            dirtied_keys=(),
            owner_domains=frozenset({"withdrawal_sequencer"}),
            flipped_hard_verdicts=(),
            introduces_structure=False,
            structure_scope="none",
            changed_policy_axes=frozenset(),
            changes_plan_identity_axis=False,
            adds_or_removes_owner_domain=False,
            adds_cross_owner_dependency=False,
            invalidates_global_invariant=False,
            missing_owner_for_changed_node=False,
            touched_rebuild_boundaries=frozenset(),
            touches_owner_authored_surface=False,
            dirtied_boundary_fraction=0.0,
        )
        defaults.update(overrides)
        return BlastRadius(**defaults)

    def test_missing_owner(self):
        br = self._br(missing_owner_for_changed_node=True)
        tier, reason = classify(br)
        assert tier == Tier.FULL_REBUILD
        assert "owner" in reason.lower()

    def test_plan_identity_axis(self):
        br = self._br(changes_plan_identity_axis=True)
        tier, reason = classify(br)
        assert tier == Tier.FULL_REBUILD
        assert "identity" in reason.lower() or "policy" in reason.lower()

    def test_adds_or_removes_owner_domain(self):
        br = self._br(adds_or_removes_owner_domain=True)
        tier, reason = classify(br)
        assert tier == Tier.FULL_REBUILD
        assert "owner" in reason.lower() or "domain" in reason.lower()

    def test_adds_cross_owner_dependency(self):
        br = self._br(adds_cross_owner_dependency=True)
        tier, reason = classify(br)
        assert tier == Tier.FULL_REBUILD
        assert "cross" in reason.lower() or "owner" in reason.lower()

    def test_invalidates_global_invariant(self):
        br = self._br(invalidates_global_invariant=True)
        tier, reason = classify(br)
        assert tier == Tier.FULL_REBUILD
        assert "invariant" in reason.lower()

    def test_plan_basis_hard_verdict_flip(self):
        br = self._br(flipped_hard_verdicts=(HardVerdictFlip(key="allocation.target", severity="plan_basis"),))
        tier, reason = classify(br)
        assert tier == Tier.FULL_REBUILD
        assert "plan" in reason.lower() or "basis" in reason.lower()

    def test_policy_crosses_two_rebuild_boundaries(self):
        br = self._br(
            touched_rebuild_boundaries=frozenset({"withdrawal", "concentration"}),
            changed_policy_axes=frozenset({"risk"}),
        )
        tier, reason = classify(br)
        assert tier == Tier.FULL_REBUILD
        assert "rebuild" in reason.lower() or "boundary" in reason.lower() or "policy" in reason.lower()

    def test_unsupplied_figure_change(self):
        """Unsupplied value on an owner_authored node → T2 via missing_owner path."""
        br = self._br(missing_owner_for_changed_node=True)
        tier, reason = classify(br)
        assert tier == Tier.FULL_REBUILD

    def test_precedence_missing_owner_beats_identity(self):
        """missing_owner fires BEFORE identity axis in the precedence table."""
        br = self._br(missing_owner_for_changed_node=True, changes_plan_identity_axis=True)
        tier, reason = classify(br)
        assert tier == Tier.FULL_REBUILD
        # reason should name the FIRST trigger (missing owner)
        assert "owner" in reason.lower()


# ---------------------------------------------------------------------------
# classify() — T1 triggers
# ---------------------------------------------------------------------------

class TestClassifyT1:
    def _br(self, **overrides) -> BlastRadius:
        defaults = dict(
            dirtied_keys=(),
            owner_domains=frozenset({"withdrawal_sequencer"}),
            flipped_hard_verdicts=(),
            introduces_structure=False,
            structure_scope="none",
            changed_policy_axes=frozenset(),
            changes_plan_identity_axis=False,
            adds_or_removes_owner_domain=False,
            adds_cross_owner_dependency=False,
            invalidates_global_invariant=False,
            missing_owner_for_changed_node=False,
            touched_rebuild_boundaries=frozenset(),
            touches_owner_authored_surface=False,
            dirtied_boundary_fraction=0.0,
        )
        defaults.update(overrides)
        return BlastRadius(**defaults)

    def test_introduces_structure(self):
        br = self._br(introduces_structure=True, structure_scope="local_owner")
        tier, reason = classify(br)
        assert tier == Tier.BOUNDED_REDERIVE
        assert "structure" in reason.lower() or "owner" in reason.lower()

    def test_multi_owner_domains(self):
        br = self._br(owner_domains=frozenset({"withdrawal_sequencer", "household_budget"}))
        tier, reason = classify(br)
        assert tier == Tier.BOUNDED_REDERIVE
        assert "owner" in reason.lower() or "multi" in reason.lower()

    def test_flipped_hard_verdicts_localized(self):
        br = self._br(flipped_hard_verdicts=(HardVerdictFlip(key="spend.total", severity="localized"),))
        tier, reason = classify(br)
        assert tier == Tier.BOUNDED_REDERIVE
        assert "verdict" in reason.lower() or "hard" in reason.lower() or "localized" in reason.lower()

    def test_touches_owner_authored_surface(self):
        br = self._br(touches_owner_authored_surface=True)
        tier, reason = classify(br)
        assert tier == Tier.BOUNDED_REDERIVE
        assert "owner" in reason.lower() or "authored" in reason.lower() or "surface" in reason.lower()

    def test_large_blast_radius_fraction(self):
        br = self._br(dirtied_boundary_fraction=0.5)  # > 0.34 default
        tier, reason = classify(br)
        assert tier == Tier.BOUNDED_REDERIVE
        assert "blast" in reason.lower() or "large" in reason.lower() or "radius" in reason.lower()

    def test_exactly_at_threshold_is_t0(self):
        """dirtied_boundary_fraction == 0.34 is NOT > threshold, so falls to T0."""
        br = self._br(dirtied_boundary_fraction=0.34)
        tier, _ = classify(br)
        assert tier == Tier.SCOPED_EDIT

    def test_one_rebuild_boundary_with_policy_change_no_t2(self):
        """T2 requires > 1 rebuild boundaries; single boundary with policy change → T1 or T0."""
        br = self._br(
            touched_rebuild_boundaries=frozenset({"withdrawal"}),
            changed_policy_axes=frozenset({"risk"}),
        )
        tier, _ = classify(br)
        # Should NOT be T2 (only 1 boundary, not >1).
        assert tier != Tier.FULL_REBUILD


# ---------------------------------------------------------------------------
# classify() — T0 fallthrough
# ---------------------------------------------------------------------------

class TestClassifyT0:
    def test_t0_fallthrough_all_clear(self):
        br = BlastRadius(
            dirtied_keys=("spend.total",),
            owner_domains=frozenset({"household_budget"}),
            flipped_hard_verdicts=(),
            introduces_structure=False,
            structure_scope="none",
            changed_policy_axes=frozenset({"withdrawal"}),
            changes_plan_identity_axis=False,
            adds_or_removes_owner_domain=False,
            adds_cross_owner_dependency=False,
            invalidates_global_invariant=False,
            missing_owner_for_changed_node=False,
            touched_rebuild_boundaries=frozenset(),
            touches_owner_authored_surface=False,
            dirtied_boundary_fraction=0.1,
        )
        tier, reason = classify(br)
        assert tier == Tier.SCOPED_EDIT
        assert "deterministic" in reason.lower() or "localized" in reason.lower() or "edit" in reason.lower()

    def test_t0_empty_graph_change(self):
        br = BlastRadius(
            dirtied_keys=(),
            owner_domains=frozenset(),
            flipped_hard_verdicts=(),
            introduces_structure=False,
            structure_scope="none",
            changed_policy_axes=frozenset(),
            changes_plan_identity_axis=False,
            adds_or_removes_owner_domain=False,
            adds_cross_owner_dependency=False,
            invalidates_global_invariant=False,
            missing_owner_for_changed_node=False,
            touched_rebuild_boundaries=frozenset(),
            touches_owner_authored_surface=False,
            dirtied_boundary_fraction=0.0,
        )
        tier, reason = classify(br)
        assert tier == Tier.SCOPED_EDIT


# ---------------------------------------------------------------------------
# size_blast_radius() — integration tests with real DerivationGraph
# ---------------------------------------------------------------------------

class TestSizeBlastRadius:
    def test_scoped_scalar_change_t0(self):
        """Single INPUT change in one owner domain (savings.monthly) → small blast radius → T0.

        Uses savings.* keys: deterministic, hard_verdict_severity=None, single owner.
        No plan_basis flips, no multi-owner → should classify T0.
        """
        g = _scalar_graph()
        changes = [ChangeRequest(node_key="savings.monthly", new_value=6_000.0, supplies_value=True)]
        br = size_blast_radius(g, changes)

        # savings.total and savings.net are dirtied
        assert "savings.total" in br.dirtied_keys
        assert "savings.net" in br.dirtied_keys

        tier, reason = classify(br)
        assert tier == Tier.SCOPED_EDIT

    def test_scoped_change_does_not_mutate_original(self):
        """size_blast_radius MUST NOT mutate the original graph."""
        g = _scalar_graph()
        original_value = g.get("savings.monthly").value
        original_derived = g.get("savings.total").value

        changes = [ChangeRequest(node_key="savings.monthly", new_value=999_999.0, supplies_value=True)]
        size_blast_radius(g, changes)

        assert g.get("savings.monthly").value == original_value, "original input was mutated"
        assert g.get("savings.total").value == original_derived, "original derived was mutated"

    def test_two_owner_change_t1(self):
        """Changes across two owner domains (savings/equity_comp + fx) → BOUNDED_REDERIVE."""
        g = _two_owner_graph()
        changes = [
            ChangeRequest(node_key="savings.monthly", new_value=15.0, supplies_value=True),
            ChangeRequest(node_key="fx.rate", new_value=3.5, supplies_value=True),
        ]
        br = size_blast_radius(g, changes)
        # Two different owner domains touched (equity_comp + fx)
        assert len(br.owner_domains) >= 2
        tier, _ = classify(br)
        assert tier == Tier.BOUNDED_REDERIVE

    def test_plan_identity_change_t2(self):
        """Change to a plan-identity-axis key (retirement.risk_posture) → FULL_REBUILD."""
        g = _identity_graph()
        changes = [ChangeRequest(node_key="retirement.risk_posture", new_value="aggressive", supplies_value=True)]
        br = size_blast_radius(g, changes)
        assert br.changes_plan_identity_axis is True
        tier, reason = classify(br)
        assert tier == Tier.FULL_REBUILD
        assert "identity" in reason.lower() or "policy" in reason.lower()

    def test_unsupplied_owner_authored_change_t2(self):
        """supplies_value=False on an owner_authored node → FULL_REBUILD."""
        g = _owner_authored_graph()
        # portfolio.target_weight is owner_authored (portfolio.* prefix)
        changes = [ChangeRequest(node_key="portfolio.target_weight", new_value=None, supplies_value=False)]
        br = size_blast_radius(g, changes)
        assert br.missing_owner_for_changed_node is True
        tier, reason = classify(br)
        assert tier == Tier.FULL_REBUILD

    def test_dirtied_keys_are_transitive(self):
        """Dirtied set must include indirect dependents, not just direct children."""
        g = _scalar_graph()
        changes = [ChangeRequest(node_key="savings.monthly", new_value=6_000.0, supplies_value=True)]
        br = size_blast_radius(g, changes)
        # savings.net depends on savings.total which depends on savings.monthly
        assert "savings.net" in br.dirtied_keys

    def test_dirtied_boundary_fraction(self):
        """dirtied_boundary_fraction = |dirtied| / max(1, total_nodes)."""
        g = _scalar_graph()
        total = len(g.keys())  # 8 nodes
        changes = [ChangeRequest(node_key="savings.monthly", new_value=1.0, supplies_value=True)]
        br = size_blast_radius(g, changes)
        expected_frac = len(br.dirtied_keys) / max(1, total)
        assert abs(br.dirtied_boundary_fraction - expected_frac) < 1e-9

    def test_no_mutation_deepcopy_isolation(self):
        """Verify deepcopy isolates the clone: changing clone doesn't touch original."""
        g = _scalar_graph()
        pre_keys = list(g.keys())
        pre_monthly = g.get("savings.monthly").value
        pre_total = g.get("savings.total").value

        # Change two inputs to trigger substantial recomputation in the clone
        changes = [
            ChangeRequest(node_key="savings.monthly", new_value=1.0, supplies_value=True),
            ChangeRequest(node_key="savings.annual", new_value=1.0, supplies_value=True),
        ]
        size_blast_radius(g, changes)
        assert list(g.keys()) == pre_keys
        assert g.get("savings.monthly").value == pre_monthly, "original input was mutated"
        assert g.get("savings.total").value == pre_total, "original derived was mutated"

    def test_custom_tier_config(self):
        """TierConfig.max_scoped_boundary_fraction can be tuned."""
        # With a very small threshold even a tiny blast radius escalates to T1
        cfg = TierConfig(max_scoped_boundary_fraction=0.0)
        g = _scalar_graph()
        changes = [ChangeRequest(node_key="savings.monthly", new_value=55_000.0, supplies_value=True)]
        br = size_blast_radius(g, changes)
        # fraction > 0.0 → T1
        if br.dirtied_boundary_fraction > 0.0:
            tier, _ = classify(br, cfg=cfg)
            assert tier != Tier.SCOPED_EDIT


# ---------------------------------------------------------------------------
# Tier enum smoke
# ---------------------------------------------------------------------------

def test_tier_values():
    assert Tier.SCOPED_EDIT == "scoped_edit"
    assert Tier.BOUNDED_REDERIVE == "bounded_rederive"
    assert Tier.FULL_REBUILD == "full_rebuild"
