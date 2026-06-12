"""Phase 1b — AllocationAgent (Opus): orders/groups/paces/explains the
deterministic 1a candidates and invents no numbers (reconciled)."""
from __future__ import annotations

import json

import pytest

import argosy.agents.allocation_agent as aa
from argosy.agents.base import ModelCall
from argosy.services.allocation_engine import AllocationCandidate, AllocationLeg


def _c(kind, sym, usd, side):
    return AllocationCandidate(kind=kind, horizon="now",
        legs=(AllocationLeg(side=side, symbol=sym, account_id="ibkr",
              currency="USD", notional_usd=usd, funding_source="cash"),))


def _stub_model(monkeypatch, payload):
    async def fake_call(self, **kwargs):
        return ModelCall(text=json.dumps(payload), tokens_in=1, tokens_out=1,
                         model="claude-opus-4-7")
    monkeypatch.setattr(aa.AllocationAgent, "_call_model", fake_call)


def test_agent_defaults_to_opus():
    agent = aa.AllocationAgent(user_id="ariel")
    assert agent.model == "claude-opus-4-7"
    assert agent.require_citations is False


def test_agent_orders_and_paces(monkeypatch):
    cands = [_c("BUY", "CSPX", 1000, "BUY"), _c("SWAP", "SCHD", 500, "SELL")]
    _stub_model(monkeypatch, {"tasks": [
        {"candidate_index": 1, "horizon": "this_quarter", "pace": "lump",
         "pace_rationale": "", "rationale": "swap first"},
        {"candidate_index": 0, "horizon": "now", "pace": "tranched",
         "pace_rationale": "VIX elevated", "rationale": "deploy core"},
    ]})
    tasks = aa.order_and_explain(cands, verdicts={}, market_context={"vix": 28},
                                 user_id="ariel")
    assert [t.candidate.kind for t in tasks] == ["SWAP", "BUY"]
    assert tasks[1].pace == "tranched"
    assert tasks[1].pace_rationale == "VIX elevated"
    # seq is assigned in emitted order
    assert [t.seq for t in tasks] == [1, 2]


def test_agent_rejects_invented_candidate_index(monkeypatch):
    """An out-of-range candidate_index (the agent inventing a candidate) must
    fail loud, not silently drop or fabricate."""
    cands = [_c("BUY", "CSPX", 1000, "BUY")]
    _stub_model(monkeypatch, {"tasks": [
        {"candidate_index": 5, "horizon": "now", "pace": "lump",
         "pace_rationale": "", "rationale": "x"},
    ]})
    with pytest.raises(ValueError):
        aa.order_and_explain(cands, verdicts={}, market_context={}, user_id="ariel")


def test_agent_rejects_dropped_candidate(monkeypatch):
    """The agent must cover every candidate exactly once (reconciliation)."""
    cands = [_c("BUY", "CSPX", 1000, "BUY"), _c("SWAP", "SCHD", 500, "SELL")]
    _stub_model(monkeypatch, {"tasks": [
        {"candidate_index": 0, "horizon": "now", "pace": "lump",
         "pace_rationale": "", "rationale": "only one"},
    ]})
    with pytest.raises(ValueError):
        aa.order_and_explain(cands, verdicts={}, market_context={}, user_id="ariel")
