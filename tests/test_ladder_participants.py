"""Unit tests for RealLadderParticipants — the production LLM seam mapping.

The agents are monkeypatched (NO real claude.exe call); we assert the
stance->verdict and resolution->class mapping is faithful + the owner-role
inference. The live end-to-end run lives in a tmp_review verification script,
never in the test suite (a real call hangs the suite)."""
from __future__ import annotations

import types

import pytest

from argosy.orchestrator.flows.ladder_participants import (
    RealLadderParticipants, _owner_role_for,
)
from argosy.orchestrator.flows.negotiation_ladder import ArbiterClass, PeerVerdict
from argosy.quality.change_adjudication import (
    Author, AuthorKind, ChangeKind, ChangeRequest,
)


def _cr(node="retirement.required_real_yield_pct", value=0.035):
    return ChangeRequest(
        target_node_key=node,
        author=Author(AuthorKind.AGENT, "fund_manager"),
        kind=ChangeKind.SET_RECIPE,
        payload={"value": value},
        rationale="raise the SWR above the conservative default",
    )


class _FakeReport:
    def __init__(self, output):
        self.output = output


def _patch_owner(monkeypatch, stance):
    import argosy.agents.plan_node_owner as mod

    class _Fake:
        def __init__(self, **k):
            pass

        def run_sync(self, **k):
            return _FakeReport(types.SimpleNamespace(stance=stance, reasoning_md="because X"))

    monkeypatch.setattr(mod, "PlanNodeOwnerAgent", _Fake)


def _patch_fm(monkeypatch, resolution):
    import argosy.agents.fund_manager_dialogue_verdict as mod

    class _Fake:
        def __init__(self, **k):
            pass

        def run_sync(self, **k):
            return _FakeReport(
                types.SimpleNamespace(resolution=resolution, reasoning_md="FM says Y")
            )

    monkeypatch.setattr(mod, "FundManagerDialogueVerdictAgent", _Fake)


@pytest.mark.parametrize("stance,expected", [
    ("ACCEPT_CHANGE", PeerVerdict.B_CONCEDES),
    ("REJECT_CHANGE", PeerVerdict.UNRESOLVED),
    ("UNRESOLVED", PeerVerdict.UNRESOLVED),
])
def test_peer_round_owner_stance_mapping(monkeypatch, stance, expected):
    """The purpose-built owner agent maps DIRECTLY (no inversion): ACCEPT_CHANGE
    -> B concedes (apply); REJECT_CHANGE/UNRESOLVED -> defer to the arbiter, which
    rules direction (gap-2)."""
    _patch_owner(monkeypatch, stance)
    p = RealLadderParticipants("ariel")
    verdict, text = p.peer_round(change=_cr(), prior_turns=[], round=1)
    assert verdict is expected
    assert "because X" in text


@pytest.mark.parametrize("resolution,expected_class,expected_applies", [
    # ESCALATE -> a client decision (applies flag irrelevant / False).
    ("ESCALATE_TO_USER", ArbiterClass.GENUINE_DECISION, False),
    # Owner's rebuttal wins -> REJECT the proposed change (keep current).
    ("FM_ACCEPTS_ANALYST", ArbiterClass.EVIDENCE_RESOLVABLE, False),
    # FM stands by the proposed change -> APPLY.
    ("FM_MAINTAINS_OBJECTION", ArbiterClass.EVIDENCE_RESOLVABLE, True),
    # Revised + still open -> REJECT (don't apply an uncertain change).
    ("FM_REVISES_OBJECTION", ArbiterClass.EVIDENCE_RESOLVABLE, False),
])
def test_arbiter_resolution_mapping(monkeypatch, resolution, expected_class, expected_applies):
    _patch_fm(monkeypatch, resolution)
    p = RealLadderParticipants("ariel")
    klass, text, applies = p.arbiter(change=_cr(), prior_turns=[])
    assert klass is expected_class
    assert applies is expected_applies


def _patch_owner_raises(monkeypatch):
    import argosy.agents.plan_node_owner as mod

    class _Boom:
        def __init__(self, **k):
            pass

        def run_sync(self, **k):
            raise RuntimeError("claude.exe unavailable")

    monkeypatch.setattr(mod, "PlanNodeOwnerAgent", _Boom)


def _patch_fm_raises(monkeypatch):
    import argosy.agents.fund_manager_dialogue_verdict as mod

    class _Boom:
        def __init__(self, **k):
            pass

        def run_sync(self, **k):
            raise RuntimeError("missing required citations")

    monkeypatch.setattr(mod, "FundManagerDialogueVerdictAgent", _Boom)


def test_peer_round_unresponsive_owner_is_unresolved(monkeypatch):
    """An owner agent that errors -> UNRESOLVED (defer to the arbiter), never a
    silent concession."""
    _patch_owner_raises(monkeypatch)
    p = RealLadderParticipants("ariel")
    verdict, text = p.peer_round(change=_cr(), prior_turns=[], round=1)
    assert verdict is PeerVerdict.UNRESOLVED
    assert "unavailable" in text


def test_arbiter_failure_fails_safe_to_user(monkeypatch):
    """An arbiter that can't produce a ruling -> GENUINE_DECISION (surface to the
    client), never a silent auto-apply. Preserves the ladder guarantee."""
    _patch_fm_raises(monkeypatch)
    p = RealLadderParticipants("ariel")
    klass, text = p.arbiter(change=_cr(), prior_turns=[])
    assert klass is ArbiterClass.GENUINE_DECISION
    assert "could not adjudicate" in text


def test_peer_round_grounds_owner_in_graph_node_context(monkeypatch):
    """When a graph is bound, the owner agent receives the node's current value +
    derivation (so it judges on the real derivation, not from cold)."""
    import argosy.agents.plan_node_owner as mod
    from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind

    captured = {}

    class _Capture:
        def __init__(self, **k):
            pass

        def run_sync(self, **k):
            captured.update(k)
            return _FakeReport(types.SimpleNamespace(stance="UNRESOLVED", reasoning_md="r"))

    monkeypatch.setattr(mod, "PlanNodeOwnerAgent", _Capture)

    g = DerivationGraph()
    g.add_node(Node(key="fx.usd_nis", kind=NodeKind.INPUT, value=3.7))
    g.add_node(Node(key="retirement.required_real_yield_pct", kind=NodeKind.INPUT, value=0.03))
    p = RealLadderParticipants("ariel", graph=g)
    p.peer_round(change=_cr(), prior_turns=[], round=1)
    assert captured["node_key"] == "retirement.required_real_yield_pct"
    assert "0.03" in captured["current_value"]
    assert captured["proposed_value"] == "0.035"


def test_owner_role_inference():
    assert _owner_role_for("retirement.required_real_yield_pct") == "withdrawal_sequencer"
    assert _owner_role_for("spend.fi_basis_nis") == "household_budget"
    assert _owner_role_for("concentration.nvda_cap_pct") == "concentration"
    assert _owner_role_for("fx.usd_nis") == "fx"
    assert _owner_role_for("unknown.node") == "withdrawal_sequencer"  # default


def test_owner_role_override(monkeypatch):
    # owner_role now only frames the ARBITER; peer_round uses the node-keyed
    # owner agent. Smoke: construction with the override + a REJECT_CHANGE
    # owner verdict still defers to the arbiter.
    _patch_owner(monkeypatch, "REJECT_CHANGE")
    p = RealLadderParticipants("ariel", owner_role="custom_role")
    verdict, _ = p.peer_round(change=_cr(node="spend.x"), prior_turns=[], round=1)
    assert verdict is PeerVerdict.UNRESOLVED
