import pytest

from argosy.quality.change_adjudication import (
    ChangeRequest, ChangeKind, Author, AuthorKind,
    OwnershipMap, NodeClass,
    adjudicate, AdjudicationOutcome, Disposition,
    HardNodeError, assert_resolvable,
)
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind


def _graph():
    g = DerivationGraph()
    g.add_node(Node(key="liquid_nw_nis", kind=NodeKind.INPUT, value=11_687_926))
    g.add_node(Node(key="swr_pct", kind=NodeKind.INPUT, value=0.035))
    g.add_node(Node(key="fi_margin_liquid_nis", kind=NodeKind.DERIVED,
                    inputs=("liquid_nw_nis", "swr_pct"),
                    recipe=lambda i: i["liquid_nw_nis"]))
    return g


def test_change_request_construction_user_author():
    cr = ChangeRequest(
        target_node_key="swr_pct",
        author=Author(kind=AuthorKind.USER, role="user"),
        kind=ChangeKind.SET_INPUT,
        payload={"value": 0.035},
        rationale="I want a more conservative withdrawal rate.",
    )
    assert cr.target_node_key == "swr_pct"
    assert cr.author.kind is AuthorKind.USER
    assert cr.kind is ChangeKind.SET_INPUT
    assert cr.payload["value"] == 0.035


def test_change_request_construction_agent_author():
    cr = ChangeRequest(
        target_node_key="fi_margin_liquid_nis",
        author=Author(kind=AuthorKind.AGENT, role="fund_manager"),
        kind=ChangeKind.OBJECTION,
        payload={},
        rationale="FI margin looks too thin under the bear track.",
    )
    assert cr.author.kind is AuthorKind.AGENT
    assert cr.author.role == "fund_manager"
    assert cr.kind is ChangeKind.OBJECTION


def test_owner_of_default_for_input_is_user():
    g = _graph()
    om = OwnershipMap(g)
    assert om.owner_of("liquid_nw_nis") == "user"


def test_explicit_owner_overrides_default():
    g = _graph()
    om = OwnershipMap(g, owners={"swr_pct": "fund_manager"})
    assert om.owner_of("swr_pct") == "fund_manager"


def test_node_class_derived_is_not_editable():
    g = _graph()
    om = OwnershipMap(g)
    assert om.classify("fi_margin_liquid_nis") is NodeClass.DERIVED
    assert om.classify("liquid_nw_nis") is NodeClass.INPUT


def test_hard_node_flagged_by_registry():
    g = _graph()
    om = OwnershipMap(g, hard_node_keys={"fi_margin_liquid_nis"})
    assert om.is_hard("fi_margin_liquid_nis") is True
    assert om.is_hard("swr_pct") is False


def test_setting_a_derived_value_is_rejected():
    g = _graph()
    om = OwnershipMap(g)
    cr = ChangeRequest(
        target_node_key="fi_margin_liquid_nis",
        author=Author(kind=AuthorKind.AGENT, role="codex"),
        kind=ChangeKind.SET_DERIVED,
        payload={"value": 999_999},
        rationale="just make the margin bigger",
    )
    out = adjudicate(cr, om)
    assert out.disposition is Disposition.REJECTED
    assert "change the inputs or the recipe" in out.reason


def test_set_input_on_a_derived_node_is_also_rejected():
    g = _graph()
    om = OwnershipMap(g)
    cr = ChangeRequest(
        target_node_key="fi_margin_liquid_nis",
        author=Author(kind=AuthorKind.USER, role="user"),
        kind=ChangeKind.SET_INPUT,
        payload={"value": 5},
        rationale="",
    )
    out = adjudicate(cr, om)
    assert out.disposition is Disposition.REJECTED


def test_recipe_change_routes_to_ladder():
    g = _graph()
    om = OwnershipMap(g, recipe_node_keys={"swr_pct"})
    cr = ChangeRequest(
        target_node_key="swr_pct",
        author=Author(kind=AuthorKind.AGENT, role="plan_critique"),
        kind=ChangeKind.SET_RECIPE,
        payload={"value": 0.04},
        rationale="3.5% SWR is over-conservative for a 30y horizon.",
    )
    out = adjudicate(cr, om)
    assert out.disposition is Disposition.NEEDS_LADDER


def test_plain_input_change_is_accepted():
    g = _graph()
    om = OwnershipMap(g)
    cr = ChangeRequest(
        target_node_key="liquid_nw_nis",
        author=Author(kind=AuthorKind.AGENT, role="portfolio_ingest"),
        kind=ChangeKind.SET_INPUT,
        payload={"value": 11_900_000},
        rationale="refreshed holdings snapshot",
    )
    out = adjudicate(cr, om)
    assert out.disposition is Disposition.ACCEPTED


def test_verdict_flipping_input_change_needs_audit():
    g = _graph()
    om = OwnershipMap(
        g, hard_node_keys={"fi_margin_liquid_nis"},
    )
    cr = ChangeRequest(
        target_node_key="liquid_nw_nis",
        author=Author(kind=AuthorKind.AGENT, role="plan_critique"),
        kind=ChangeKind.SET_INPUT,
        payload={"value": 50_000_000},
        rationale="",
    )
    out = adjudicate(cr, om, flips_hard_verdict=True)
    assert out.disposition is Disposition.NEEDS_AUDIT
    assert "audited" in out.reason


def test_verdict_flipping_input_with_evidence_still_audited_not_accepted():
    g = _graph()
    om = OwnershipMap(g, hard_node_keys={"fi_margin_liquid_nis"})
    cr = ChangeRequest(
        target_node_key="liquid_nw_nis",
        author=Author(kind=AuthorKind.USER, role="user"),
        kind=ChangeKind.SET_INPUT,
        payload={"value": 50_000_000},
        rationale="inheritance received, see bank statement",
    )
    out = adjudicate(cr, om, flips_hard_verdict=True)
    assert out.disposition is Disposition.NEEDS_AUDIT


def test_hard_node_cannot_be_agreed_away_by_concession():
    g = _graph()
    om = OwnershipMap(g, hard_node_keys={"fi_margin_liquid_nis"})
    with pytest.raises(HardNodeError):
        assert_resolvable(
            target_node_key="fi_margin_liquid_nis",
            owners=om,
            resolution_kind="concede_value",
        )


def test_hard_node_resolvable_by_input_fix():
    g = _graph()
    om = OwnershipMap(g, hard_node_keys={"fi_margin_liquid_nis"})
    assert_resolvable(
        target_node_key="fi_margin_liquid_nis",
        owners=om,
        resolution_kind="fix_input",
    )


def test_soft_node_can_be_conceded():
    g = _graph()
    om = OwnershipMap(g)  # nothing marked hard
    assert_resolvable(
        target_node_key="swr_pct",
        owners=om,
        resolution_kind="concede_value",
    )
