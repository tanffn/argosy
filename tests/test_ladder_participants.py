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


def _patch_analyst(monkeypatch, stance):
    import argosy.agents.analyst_responder as mod

    class _Fake:
        def __init__(self, **k):
            pass

        def run_sync(self, **k):
            return _FakeReport(types.SimpleNamespace(stance=stance, reasoning_md="because X"))

    monkeypatch.setattr(mod, "AnalystResponderAgent", _Fake)


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


@pytest.mark.parametrize("stance", ["CONCEDE", "REBUT", "CLARIFY"])
def test_peer_round_always_defers_to_arbiter(monkeypatch, stance):
    """Gap (1): the reused agent's CONCEDE/REBUT label inverts in the ladder
    frame, so peer_round NEVER auto-concedes — it captures the owner's reply as
    defense and returns UNRESOLVED so the FM arbiter rules. (A CONCEDE that the
    live agent used to REJECT a change must not auto-apply it.)"""
    _patch_analyst(monkeypatch, stance)
    p = RealLadderParticipants("ariel")
    verdict, text = p.peer_round(change=_cr(), prior_turns=[], round=1)
    assert verdict is PeerVerdict.UNRESOLVED
    assert "because X" in text
    assert stance in text  # the raw stance is preserved for the arbiter


@pytest.mark.parametrize("resolution,expected", [
    ("ESCALATE_TO_USER", ArbiterClass.GENUINE_DECISION),
    ("FM_ACCEPTS_ANALYST", ArbiterClass.EVIDENCE_RESOLVABLE),
    ("FM_MAINTAINS_OBJECTION", ArbiterClass.EVIDENCE_RESOLVABLE),
    ("FM_REVISES_OBJECTION", ArbiterClass.EVIDENCE_RESOLVABLE),
])
def test_arbiter_resolution_mapping(monkeypatch, resolution, expected):
    _patch_fm(monkeypatch, resolution)
    p = RealLadderParticipants("ariel")
    klass, text = p.arbiter(change=_cr(), prior_turns=[])
    assert klass is expected


def _patch_analyst_raises(monkeypatch):
    import argosy.agents.analyst_responder as mod

    class _Boom:
        def __init__(self, **k):
            pass

        def run_sync(self, **k):
            raise RuntimeError("claude.exe unavailable")

    monkeypatch.setattr(mod, "AnalystResponderAgent", _Boom)


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
    _patch_analyst_raises(monkeypatch)
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


def test_owner_role_inference():
    assert _owner_role_for("retirement.required_real_yield_pct") == "withdrawal_sequencer"
    assert _owner_role_for("spend.fi_basis_nis") == "household_budget"
    assert _owner_role_for("concentration.nvda_cap_pct") == "concentration"
    assert _owner_role_for("fx.usd_nis") == "fx"
    assert _owner_role_for("unknown.node") == "withdrawal_sequencer"  # default


def test_owner_role_override(monkeypatch):
    _patch_analyst(monkeypatch, "REBUT")
    p = RealLadderParticipants("ariel", owner_role="custom_role")
    # No exception; override is used (smoke — the role threads into the prompt).
    verdict, _ = p.peer_round(change=_cr(node="spend.x"), prior_turns=[], round=1)
    assert verdict is PeerVerdict.UNRESOLVED
