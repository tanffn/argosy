"""Phase 3b-2 — owner remediation seam: a routed objection becomes a concrete fix
(figure value change / prose fix / decline), proposed by the owner (injected)."""
from __future__ import annotations

import pytest

from argosy.quality.change_adjudication import (
    Author, AuthorKind, ChangeKind, ChangeRequest,
)
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.finding_remediation import (
    RemediationProposal,
    propose_remediations,
)


def _objection(node_key, *, owner="retirement_fi", detail="d"):
    return ChangeRequest(
        target_node_key=node_key,
        author=Author(kind=AuthorKind.AGENT, role="whole_artifact_reader"),
        kind=ChangeKind.OBJECTION,
        payload={"owner_role": owner, "surfaces_cited": ["x"]},
        rationale=detail,
    )


def _graph():
    g = DerivationGraph()
    g.add_node(Node(key="retirement.earliest_safe_age", kind=NodeKind.INPUT, value=46.0))
    return g


class _FakeProposer:
    def __init__(self, proposal):
        self._p = proposal
        self.seen = []

    def propose(self, **kw):
        self.seen.append(kw)
        return self._p


def test_set_value_becomes_set_input_change_request():
    p = _FakeProposer(RemediationProposal(kind="set_value", value=48.0,
                                          rationale="bridge funds spend to 48"))
    res = propose_remediations([_objection("retirement.earliest_safe_age")],
                               proposer=p, graph=_graph())
    assert len(res.value_change_requests) == 1
    cr = res.value_change_requests[0]
    assert cr.kind is ChangeKind.SET_INPUT
    assert cr.payload["value"] == 48.0
    assert cr.target_node_key == "retirement.earliest_safe_age"
    assert not res.prose_fixes and not res.declines
    # the owner saw the node's current value + the finding detail
    assert p.seen[0]["current_value"] == 46.0
    assert p.seen[0]["finding_detail"] == "d"


def test_prose_fix_is_routed_to_reconcile_not_a_value_change():
    p = _FakeProposer(RemediationProposal(
        kind="prose_fix", instruction="age 46 is correct; fix the withdrawal narrative"))
    res = propose_remediations([_objection("retirement.earliest_safe_age")],
                               proposer=p, graph=_graph())
    assert not res.value_change_requests
    assert len(res.prose_fixes) == 1
    assert "withdrawal narrative" in res.prose_fixes[0]["instruction"]


def test_decline_is_recorded_not_dropped():
    p = _FakeProposer(RemediationProposal(kind="decline", rationale="finding is wrong"))
    res = propose_remediations([_objection("retirement.earliest_safe_age")],
                               proposer=p, graph=_graph())
    assert not res.value_change_requests and not res.prose_fixes
    assert res.declines and res.declines[0]["rationale"] == "finding is wrong"


def test_proposer_error_is_a_decline_not_a_crash():
    class _Boom:
        def propose(self, **kw):
            raise RuntimeError("owner offline")

    res = propose_remediations([_objection("retirement.earliest_safe_age")],
                               proposer=_Boom(), graph=_graph())
    assert res.declines and "owner offline" in res.declines[0]["rationale"]


def test_set_value_requires_a_value():
    with pytest.raises(ValueError):
        RemediationProposal(kind="set_value", value=None)
    with pytest.raises(ValueError):
        RemediationProposal(kind="bogus")
