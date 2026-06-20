"""Phase 3a — route a whole-artifact reader finding to its OWNER role (+ target
figure node), the deterministic spine of "compliance routes findings to owners"."""
from __future__ import annotations

from argosy.quality.change_adjudication import (
    AuthorKind, ChangeKind, Disposition, OwnershipMap, adjudicate,
)
from argosy.quality.coherence.surface_registry import SUBJECT_REGISTRY
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.figure_registry import OwnerRole
from argosy.quality.live_surfaces import CANONICAL_SUBJECT_NODE
from argosy.quality.finding_router import (
    RoutedFinding,
    SUBJECT_OWNER_FALLBACK,
    route_finding,
    route_verdict,
    subject_owner,
    subject_target_node,
    to_change_request,
)
from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    CoherenceFinding,
    WholeArtifactVerdict,
)


def _f(subject, sev="BLOCKER", detail="d"):
    return CoherenceFinding(
        kind="contradiction", severity=sev, detail=detail,
        surfaces_cited=["x"], subject_type=subject,
    )


def test_figure_subjects_owner_comes_from_registry():
    assert subject_owner("fi_capital_sufficiency") is OwnerRole.RETIREMENT_FI
    assert subject_owner("retirement_age_headline") is OwnerRole.RETIREMENT_FI
    assert subject_owner("fi_crossing") is OwnerRole.RETIREMENT_FI
    assert subject_owner("net_worth_liquid") is OwnerRole.BALANCE_SHEET
    assert subject_owner("net_worth_total") is OwnerRole.BALANCE_SHEET
    assert subject_owner("us_situs_estate") is OwnerRole.ESTATE
    assert subject_owner("retention_at_vest") is OwnerRole.TAX
    assert subject_owner("retention_capital_track") is OwnerRole.TAX
    assert subject_target_node("net_worth_liquid") == "portfolio.liquid_net_worth_nis"
    assert subject_target_node("fi_capital_sufficiency") == "retirement.fi_margin_signed_nis"


def test_prose_policy_subjects_have_explicit_owners():
    assert subject_owner("rsu_vest_policy") is OwnerRole.EQUITY_COMP
    assert subject_owner("tranche_execution_gate") is OwnerRole.INVESTMENT
    assert subject_owner("sgln_ucits_membership") is OwnerRole.INVESTMENT
    assert subject_target_node("rsu_vest_policy") is None


def test_every_registry_and_canonical_subject_is_routable():
    # Explicit contract: the router covers BOTH the reader's coherence taxonomy
    # AND the canonical-surface subjects — a new subject in either fails until routed.
    for subject in set(SUBJECT_REGISTRY) | set(CANONICAL_SUBJECT_NODE):
        assert subject_owner(subject) is not None, f"unrouted subject: {subject}"


def test_us_situs_target_is_the_graph_node_key():
    # The target must be the key present in the incremental graph (US_SITUS_KEY),
    # and owner_for still resolves it to ESTATE.
    assert subject_target_node("us_situs_estate") == "concentration.us_situs_estate_nis"
    assert subject_owner("us_situs_estate") is OwnerRole.ESTATE


def test_unknown_subject_is_unrouted_not_crash():
    assert subject_owner("") is None
    assert subject_owner("not_a_subject") is None


def test_route_finding_figure_subject():
    r = route_finding(_f("retirement_age_headline"))
    assert isinstance(r, RoutedFinding)
    assert r.owner is OwnerRole.RETIREMENT_FI
    assert r.target_node_key == "retirement.earliest_safe_age"
    assert r.severity == "BLOCKER"


def test_route_finding_unrouted_subject_returns_none():
    assert route_finding(_f("")) is None
    assert route_finding(_f("mystery")) is None


def test_route_verdict_splits_routed_and_unroutable():
    verdict = WholeArtifactVerdict(overall_assessment="BLOCK", findings=[
        _f("retirement_age_headline"),
        _f("tranche_execution_gate"),
        _f("mystery"),                 # unroutable
        _f("net_worth_liquid", sev="YELLOW"),
    ])
    routed, unroutable = route_verdict(verdict)  # default: all severities
    owners = {r.owner for r in routed}
    assert OwnerRole.RETIREMENT_FI in owners and OwnerRole.INVESTMENT in owners
    assert OwnerRole.BALANCE_SHEET in owners
    assert len(routed) == 3 and len(unroutable) == 1
    assert unroutable[0].subject_type == "mystery"
    routed_blockers, _ = route_verdict(verdict, severities=("BLOCKER",))
    assert routed_blockers and all(r.severity == "BLOCKER" for r in routed_blockers)


def test_to_change_request_objection_for_figure_subject():
    r = route_finding(_f("net_worth_liquid"))
    cr = to_change_request(r)
    assert cr is not None
    assert cr.target_node_key == "portfolio.liquid_net_worth_nis"
    assert cr.kind is ChangeKind.OBJECTION
    assert cr.author.kind is AuthorKind.AGENT
    assert cr.author.role == "whole_artifact_reader"
    assert cr.payload["owner_role"] == OwnerRole.BALANCE_SHEET.value  # owner not lost
    assert to_change_request(route_finding(_f("rsu_vest_policy"))) is None


def test_objection_change_request_routes_to_owner_not_rejected():
    """The OBJECTION CR must reach the owner (NEEDS_LADDER), not be REJECTED as a
    non-editable DerivedValue nor ACCEPTED as an edit (codex impl review #1)."""
    cr = to_change_request(route_finding(_f("fi_capital_sufficiency")))
    assert cr is not None and cr.kind is ChangeKind.OBJECTION
    # Even when the target is a DERIVED node, an objection routes to the owner.
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1.0))
    g.add_node(Node(key=cr.target_node_key, kind=NodeKind.DERIVED, value=0.0,
                    inputs=("x",), recipe=lambda i: i["x"], compute_version="v1"))
    owners = OwnershipMap(g)
    assert adjudicate(cr, owners).disposition is Disposition.NEEDS_LADDER
