"""Tests for POST /api/plan/refine — dry-run refinement preview endpoint.

Monkeypatches build_base_graph and run_refinement to avoid real DB/graph
hydration, so these tests run without a seeded plan. The production code
path calls the real functions; the mocks are test-only.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers to build minimal fake graph + decision objects
# ---------------------------------------------------------------------------

def _make_fake_graph():
    from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind

    g = DerivationGraph()
    g.add_node(Node(key="portfolio.fx_usd_nis", kind=NodeKind.INPUT, value=3.7))
    g.recompute()
    return g


def _make_fake_decision(tier_value="scoped_edit", dirtied=("portfolio.net_worth_nis",)):
    from argosy.quality.blast_radius import BlastRadius, Tier
    from argosy.quality.refinement import RefinementDecision

    br = BlastRadius(
        dirtied_keys=tuple(dirtied),
        owner_domains=frozenset({"portfolio"}),
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
        dirtied_boundary_fraction=0.1,
    )
    return RefinementDecision(
        tier=Tier(tier_value),
        reason="deterministic localized edit",
        blast_radius=br,
        invariant_report=None,
        forced_by_invariant=False,
    )


def _make_full_rebuild_decision():
    from argosy.quality.blast_radius import BlastRadius, Tier
    from argosy.quality.refinement import RefinementDecision

    br = BlastRadius(
        dirtied_keys=(),
        owner_domains=frozenset({"retirement"}),
        flipped_hard_verdicts=(),
        introduces_structure=False,
        structure_scope="none",
        changed_policy_axes=frozenset({"risk"}),
        changes_plan_identity_axis=True,
        adds_or_removes_owner_domain=False,
        adds_cross_owner_dependency=False,
        invalidates_global_invariant=False,
        missing_owner_for_changed_node=False,
        touched_rebuild_boundaries=frozenset(),
        touches_owner_authored_surface=False,
        dirtied_boundary_fraction=0.0,
    )
    return RefinementDecision(
        tier=Tier.FULL_REBUILD,
        reason="changes plan identity / core policy axis",
        blast_radius=br,
        invariant_report=None,
        forced_by_invariant=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_dry_run_false_returns_501(client_with_db):
    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [{"node_key": "portfolio.fx_usd_nis", "new_value": 3.9}],
            "dry_run": False,
        },
    )
    assert r.status_code == 501
    assert "not yet enabled" in r.json()["detail"].lower()


def test_dry_run_returns_decision_dto(client_with_db, monkeypatch):
    """Happy path: monkeypatched build_base_graph + run_refinement returns DTO."""
    import argosy.api.routes.plan as plan_mod

    fake_graph = _make_fake_graph()
    fake_decision = _make_fake_decision()

    monkeypatch.setattr(
        "argosy.orchestrator.flows.incremental_plan.build_base_graph",
        lambda session, user_id, *, decision_run_id: fake_graph,
    )
    monkeypatch.setattr(
        "argosy.quality.refinement.run_refinement",
        lambda graph, crs, *, pre_doc=None, post_doc=None, cfg=None: fake_decision,
    )

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [{"node_key": "portfolio.fx_usd_nis", "new_value": 3.9}],
            "dry_run": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tier"] in {"scoped_edit", "bounded_rederive", "full_rebuild"}
    assert isinstance(body["reason"], str) and body["reason"]
    assert isinstance(body["dirtied_count"], int)
    assert isinstance(body["owner_domains"], list)
    assert body["forced_by_invariant"] is False
    assert body["invariant_ok"] is None
    assert body["invariant_violations"] == []
    assert isinstance(body["summary"], str) and body["summary"]


def test_plan_identity_key_returns_full_rebuild(client_with_db, monkeypatch):
    """A change to a plan-identity key must return tier=full_rebuild."""
    fake_graph = _make_fake_graph()
    fake_decision = _make_full_rebuild_decision()

    monkeypatch.setattr(
        "argosy.orchestrator.flows.incremental_plan.build_base_graph",
        lambda session, user_id, *, decision_run_id: fake_graph,
    )
    monkeypatch.setattr(
        "argosy.quality.refinement.run_refinement",
        lambda graph, crs, *, pre_doc=None, post_doc=None, cfg=None: fake_decision,
    )

    r = client_with_db.post(
        "/api/plan/refine",
        json={
            "user_id": "ariel",
            "changes": [{"node_key": "retirement.risk_posture", "new_value": "conservative"}],
            "dry_run": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["tier"] == "full_rebuild"
