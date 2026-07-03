"""Tests for argosy.quality.refinement — read-only refinement orchestration.

TDD: tests written BEFORE implementation (red → green).

Coverage:
  1. Scoped change + ok post_doc → SCOPED_EDIT, forced_by_invariant=False.
  2. CRITICAL: classify says SCOPED/BOUNDED but post_doc has invariant violation
     → decision.tier == FULL_REBUILD, forced_by_invariant=True, reason mentions invariant.
  3. Classifier itself sends FULL_REBUILD (plan-identity node) → stays FULL_REBUILD.
  4. post_doc=None → invariant_report=None, tier == classifier's tier (net inert).
  5. summary() renders the expected one-liner, with [invariant-forced] appended only when forced.
"""
from __future__ import annotations

import types

import pytest

from argosy.quality.blast_radius import ChangeRequest, Tier, TierConfig
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.refinement import RefinementDecision, run_refinement, summary


# ---------------------------------------------------------------------------
# Minimal graph helpers
# ---------------------------------------------------------------------------

def _scoped_graph() -> DerivationGraph:
    """Two savings.* INPUT nodes + one DERIVED. Changing savings.monthly
    dirtied=1/3 nodes (< 0.34 threshold), single owner → classify → SCOPED_EDIT."""
    g = DerivationGraph()
    g.add_node(Node(key="savings.monthly", kind=NodeKind.INPUT, value=5_000.0))
    g.add_node(Node(key="savings.annual", kind=NodeKind.INPUT, value=60_000.0))
    g.add_node(Node(
        key="savings.total",
        kind=NodeKind.DERIVED,
        inputs=("savings.monthly",),
        recipe=lambda iv: iv["savings.monthly"] * 12,
        compute_version="v1",
    ))
    g.recompute()
    return g


def _plan_identity_graph() -> DerivationGraph:
    """Contains a plan-identity node (retirement.risk_posture) that triggers FULL_REBUILD via classifier."""
    g = DerivationGraph()
    g.add_node(Node(key="retirement.risk_posture", kind=NodeKind.INPUT, value="growth"))
    g.add_node(Node(key="savings.monthly", kind=NodeKind.INPUT, value=5_000.0))
    g.recompute()
    return g


# ---------------------------------------------------------------------------
# Plan doc helpers
# ---------------------------------------------------------------------------

def _ok_doc():
    """Plan doc that passes all invariants: classes sum to 100%, no NVDA exposure."""
    cls_a = types.SimpleNamespace(
        target_pct=60.0,
        instruments=[types.SimpleNamespace(symbol="CSPX", weight_within_class_pct=100.0)],
    )
    cls_b = types.SimpleNamespace(
        target_pct=40.0,
        instruments=[types.SimpleNamespace(symbol="VWRA", weight_within_class_pct=100.0)],
    )
    return types.SimpleNamespace(classes=[cls_a, cls_b], nvda_cap_pct=13.0)


def _broken_doc():
    """Plan doc that fails the allocation_sum invariant: classes sum to 110% (not 100%)."""
    cls_a = types.SimpleNamespace(
        target_pct=70.0,
        instruments=[types.SimpleNamespace(symbol="CSPX", weight_within_class_pct=100.0)],
    )
    cls_b = types.SimpleNamespace(
        target_pct=40.0,
        instruments=[types.SimpleNamespace(symbol="VWRA", weight_within_class_pct=100.0)],
    )
    return types.SimpleNamespace(classes=[cls_a, cls_b], nvda_cap_pct=13.0)


# ---------------------------------------------------------------------------
# We stub out the look-through so CSPX/VWRA carry 0% NVDA and NVDA carries 100%.
# This prevents calls to the real effective_nvda_usd (which requires DB / instrument ref)
# and makes tests hermetic.  The effective_fn override is threaded in via monkeypatch
# on the module-level default so evaluate_plan_invariants stays deterministic.
# ---------------------------------------------------------------------------

def _stub_effective_fn(sym: str, val: float) -> float:
    return val if sym.upper() == "NVDA" else 0.0


# ---------------------------------------------------------------------------
# Test 1: scoped change + ok post_doc → SCOPED_EDIT, forced_by_invariant=False
# ---------------------------------------------------------------------------

def test_scoped_change_ok_post_doc(monkeypatch):
    """A small savings.monthly change in a single-owner graph with a healthy
    post_doc lands at SCOPED_EDIT and does NOT trigger the invariant override."""
    import argosy.quality.plan_risk_kernel as prk
    monkeypatch.setattr(prk, "_default_effective_fn", _stub_effective_fn)

    g = _scoped_graph()
    cr = ChangeRequest(node_key="savings.monthly", new_value=6_000.0, supplies_value=True)
    doc = _ok_doc()

    decision = run_refinement(g, [cr], post_doc=doc)

    assert decision.tier == Tier.SCOPED_EDIT
    assert decision.forced_by_invariant is False
    assert decision.invariant_report is not None
    assert decision.invariant_report.ok is True
    assert isinstance(decision, RefinementDecision)
    # frozen — mutation must raise
    with pytest.raises((AttributeError, TypeError)):
        decision.tier = Tier.FULL_REBUILD  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 2 (CRITICAL SAFETY): classifier says SCOPED/BOUNDED but post_doc has
# invariant violation → FORCE FULL_REBUILD, forced_by_invariant=True
# ---------------------------------------------------------------------------

def test_invariant_net_overrides_classifier(monkeypatch):
    """THE SAFETY TEST: even when blast-radius classify() returns SCOPED_EDIT,
    a post_doc with a failing invariant (allocation_sum != 100%) must force
    FULL_REBUILD and set forced_by_invariant=True."""
    import argosy.quality.plan_risk_kernel as prk
    monkeypatch.setattr(prk, "_default_effective_fn", _stub_effective_fn)

    g = _scoped_graph()
    cr = ChangeRequest(node_key="savings.monthly", new_value=6_000.0, supplies_value=True)
    broken = _broken_doc()  # sums to 110%, not 100%

    decision = run_refinement(g, [cr], post_doc=broken)

    assert decision.tier == Tier.FULL_REBUILD, (
        "Invariant safety net must override classifier's SCOPED_EDIT verdict"
    )
    assert decision.forced_by_invariant is True
    # Reason must name the invariant breach
    assert "invariant" in decision.reason.lower() or "allocation" in decision.reason.lower(), (
        f"reason should mention the invariant breach, got: {decision.reason!r}"
    )
    assert decision.invariant_report is not None
    assert not decision.invariant_report.ok


# ---------------------------------------------------------------------------
# Test 3: classifier itself sends FULL_REBUILD (plan-identity node) → stays T2
# ---------------------------------------------------------------------------

def test_classifier_full_rebuild_stays(monkeypatch):
    """Changing a plan-identity node (plan.fi_age) routes to FULL_REBUILD via
    the classifier — the orchestrator must not downgrade it."""
    import argosy.quality.plan_risk_kernel as prk
    monkeypatch.setattr(prk, "_default_effective_fn", _stub_effective_fn)

    g = _plan_identity_graph()
    cr = ChangeRequest(node_key="retirement.risk_posture", new_value="balanced", supplies_value=True)
    doc = _ok_doc()

    decision = run_refinement(g, [cr], post_doc=doc)

    assert decision.tier == Tier.FULL_REBUILD
    assert decision.forced_by_invariant is False  # came from classifier, not net
    assert "plan" in decision.reason.lower() or "identity" in decision.reason.lower() or "policy" in decision.reason.lower()


# ---------------------------------------------------------------------------
# Test 4: post_doc=None → invariant_report=None, tier == classifier's tier
# ---------------------------------------------------------------------------

def test_no_post_doc_net_inert():
    """When post_doc is omitted the invariant net is inert: invariant_report is
    None and the tier is exactly what the classifier decided."""
    g = _scoped_graph()
    cr = ChangeRequest(node_key="savings.monthly", new_value=6_000.0, supplies_value=True)

    decision = run_refinement(g, [cr])  # no post_doc

    assert decision.invariant_report is None
    assert decision.forced_by_invariant is False
    assert decision.tier == Tier.SCOPED_EDIT


# ---------------------------------------------------------------------------
# Test 5: summary() renders expected one-liner
# ---------------------------------------------------------------------------

def test_summary_format_normal(monkeypatch):
    """summary() for a non-forced decision: no [invariant-forced] suffix."""
    import argosy.quality.plan_risk_kernel as prk
    monkeypatch.setattr(prk, "_default_effective_fn", _stub_effective_fn)

    g = _scoped_graph()
    cr = ChangeRequest(node_key="savings.monthly", new_value=6_000.0, supplies_value=True)
    decision = run_refinement(g, [cr], post_doc=_ok_doc())

    s = summary(decision)
    assert s.startswith("scoped_edit:") or s.startswith("SCOPED_EDIT:")
    assert "dirtied=" in s
    assert "owners=" in s
    assert "[invariant-forced]" not in s


def test_summary_format_forced(monkeypatch):
    """summary() for a forced decision appends [invariant-forced]."""
    import argosy.quality.plan_risk_kernel as prk
    monkeypatch.setattr(prk, "_default_effective_fn", _stub_effective_fn)

    g = _scoped_graph()
    cr = ChangeRequest(node_key="savings.monthly", new_value=6_000.0, supplies_value=True)
    decision = run_refinement(g, [cr], post_doc=_broken_doc())

    s = summary(decision)
    assert "[invariant-forced]" in s
