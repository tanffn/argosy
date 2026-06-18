import pytest

from argosy.orchestrator.flows.negotiation_ladder import (
    LadderTurn, TerminalState, Speaker, Stance,
    run_ladder, LadderResult, LadderParticipants, PeerVerdict, ArbiterClass,
)


def test_ladder_turn_fields():
    t = LadderTurn(
        round=1, speaker=Speaker.A, stance=Stance.PROPOSE,
        text="change swr_pct to 0.04 because the horizon is 30y",
        cited_nodes=["swr_pct"],
    )
    assert t.round == 1
    assert t.speaker is Speaker.A
    assert t.stance is Stance.PROPOSE
    assert t.cited_nodes == ["swr_pct"]


def test_terminal_states_enumerated():
    assert {s.value for s in TerminalState} >= {
        "A_conceded", "B_conceded", "arbiter_ruled",
        "escalated_to_user", "superseded",
    }


class _FakeParticipants:
    """Deterministic test double for the LLM seam."""
    def __init__(self, peer_sequence, arbiter_class=None, arbiter_ruling=""):
        self._peer = list(peer_sequence)
        self._arbiter_class = arbiter_class
        self._arbiter_ruling = arbiter_ruling
        self.peer_calls = 0
        self.arbiter_calls = 0

    def peer_round(self, *, change, prior_turns, round):
        self.peer_calls += 1
        verdict, text = self._peer[round - 1]
        return verdict, text

    def arbiter(self, *, change, prior_turns):
        self.arbiter_calls += 1
        return self._arbiter_class, self._arbiter_ruling


def _change():
    from argosy.quality.change_adjudication import (
        ChangeRequest, ChangeKind, Author, AuthorKind,
    )
    return ChangeRequest(
        target_node_key="swr_pct",
        author=Author(kind=AuthorKind.AGENT, role="plan_critique"),
        kind=ChangeKind.SET_RECIPE,
        payload={"value": 0.04},
        rationale="3.5% is over-conservative for a 30y horizon",
    )


def test_b_concedes_in_round_one():
    parts = _FakeParticipants([(PeerVerdict.B_CONCEDES, "you're right, 0.04 is defensible")])
    res = run_ladder(_change(), parts)
    assert res.terminal_state.value == "B_conceded"
    assert parts.peer_calls == 1
    assert parts.arbiter_calls == 0
    assert res.turns[0].stance.value == "propose"
    assert res.turns[-1].stance.value == "concede"


def test_a_concedes_when_rebuttal_lands():
    parts = _FakeParticipants([(PeerVerdict.A_CONCEDES, "fair, the horizon assumption was wrong")])
    res = run_ladder(_change(), parts)
    assert res.terminal_state.value == "A_conceded"
    assert parts.arbiter_calls == 0


def test_escalates_to_arbiter_after_three_unresolved_rounds():
    parts = _FakeParticipants(
        [(PeerVerdict.UNRESOLVED, "no"),
         (PeerVerdict.UNRESOLVED, "still no"),
         (PeerVerdict.UNRESOLVED, "we disagree")],
        arbiter_class=ArbiterClass.EVIDENCE_RESOLVABLE,
        arbiter_ruling="re-derive from the 30y horizon table; A's 0.04 is supported",
    )
    res = run_ladder(_change(), parts)
    assert parts.peer_calls == 3          # exactly n=3 peer rounds
    assert parts.arbiter_calls == 1       # then escalate
    assert res.terminal_state.value == "arbiter_ruled"
    assert res.arbiter_class is ArbiterClass.EVIDENCE_RESOLVABLE
    assert "re-derive" in res.turns[-1].text
    assert res.turns[-1].speaker.value == "arbiter"


def test_arbiter_routes_genuine_decision_to_user():
    parts = _FakeParticipants(
        [(PeerVerdict.UNRESOLVED, "no")] * 3,
        arbiter_class=ArbiterClass.GENUINE_DECISION,
        arbiter_ruling="this is a risk-tolerance call only the client can make",
    )
    res = run_ladder(_change(), parts)
    assert res.terminal_state.value == "escalated_to_user"
    assert res.arbiter_class is ArbiterClass.GENUINE_DECISION
    assert res.user_question  # a single boxed choice was produced
    stances = [t.stance.value for t in res.turns]
    assert "classify" in stances
    assert res.turns[-1].speaker.value == "user"
    assert res.turns[-1].stance.value == "ask"
