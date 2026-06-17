# tests/coherence/test_resolver_route.py
from argosy.quality.coherence.dispute import Dispute
from argosy.quality.coherence.resolver_route import route_dispute, RouteKind


def test_value_mismatch_routes_to_resolver():
    d = Dispute(subject_type="nvda_cap", subject_field_path="concentration.nvda_cap_pct",
                scope="person", conflict_type="value_mismatch", question="x")
    assert route_dispute(d) == RouteKind.RESOLVER


def test_policy_tension_routes_to_arbitration():
    d = Dispute(subject_type="retirement_age_headline", subject_field_path="",
                scope="person", conflict_type="policy_tension", question="x")
    assert route_dispute(d) == RouteKind.ARBITRATION


def test_untypeable_routes_to_block():
    d = Dispute(subject_type="", subject_field_path="", scope="",
                conflict_type="value_mismatch", question="x")
    assert route_dispute(d) == RouteKind.BLOCK
