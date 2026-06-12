"""Phase 2 — QuickEstimatorAgent (Sonnet): cheap single-shot triage screen that
turns a radar shortlist into go/no-go EstimatorVerdicts before the expensive
Opus fleet grading."""
from __future__ import annotations

import json

import argosy.agents.quick_estimator as qe
from argosy.agents.base import ModelCall
from argosy.services.contracts import EstimatorVerdict
from argosy.services.trend_radar import TrendCandidate


def _cand(ticker, score=80.0):
    return TrendCandidate(ticker=ticker, name=ticker, score=score,
                          families=("MOMENTUM",), reasons=("trending",),
                          price=100.0, market_cap=5e9, dollar_volume=2e8,
                          pct_change=4.0)


def _stub(monkeypatch, by_ticker):
    """by_ticker: {ticker: payload-dict} -> stub returns the matching payload."""
    async def fake_call(self, *, system, user, **kwargs):
        tk = next(t for t in by_ticker if t in user)
        return ModelCall(text=json.dumps(by_ticker[tk]), tokens_in=1,
                         tokens_out=1, model="claude-sonnet-4-6")
    monkeypatch.setattr(qe.QuickEstimatorAgent, "_call_model", fake_call)


def test_estimator_defaults_to_sonnet():
    agent = qe.QuickEstimatorAgent(user_id="ariel")
    assert agent.model == "claude-sonnet-4-6"
    assert agent.require_citations is False


def test_estimate_returns_verdict(monkeypatch):
    _stub(monkeypatch, {"PLTR": {
        "ticker": "PLTR", "go": True, "conviction": "HIGH",
        "sentiment": 0.7, "one_line": "strong momentum + AI tailwind"}})
    v = qe.estimate(_cand("PLTR"), user_id="ariel")
    assert isinstance(v, EstimatorVerdict)
    assert v.ticker == "PLTR" and v.go is True and v.conviction == "HIGH"


def test_triage_filters_no_go_low_conviction(monkeypatch):
    _stub(monkeypatch, {
        "PLTR": {"ticker": "PLTR", "go": True, "conviction": "HIGH",
                 "sentiment": 0.8, "one_line": "go"},
        "ZZZZ": {"ticker": "ZZZZ", "go": False, "conviction": "LOW",
                 "sentiment": -0.2, "one_line": "no"},
    })
    survivors = qe.triage([_cand("PLTR", 90.0), _cand("ZZZZ", 40.0)],
                          user_id="ariel")
    assert [v.ticker for v in survivors] == ["PLTR"]  # ZZZZ (go=False) filtered
