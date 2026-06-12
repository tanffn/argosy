"""Phase 2 — grade_discovery_ticker (codex #6): a single-ticker grader that wraps
run_per_ticker_analysts + one Opus synthesis into a FleetPick, with an
idempotency key and NO proposal persistence (never DecisionFlow.run)."""
from __future__ import annotations

import asyncio
import json

import pytest

import argosy.services.discovery_grader as dg
from argosy.agents.base import ModelCall
from argosy.services.contracts import FleetPick
from argosy.services.trend_radar import TrendCandidate


def _cand(ticker="PLTR"):
    return TrendCandidate(ticker=ticker, name=ticker, score=88.0,
                          families=("MOMENTUM",), reasons=("trend",),
                          price=100.0, market_cap=5e9, dollar_volume=2e8,
                          pct_change=5.0)


class _Report:
    def __init__(self, role, text):
        self.agent_role = role
        self.response_text = text


class _Result:
    def __init__(self, reports):
        self.reports = reports


def test_idempotency_key_stable_and_varies():
    k1 = dg.discovery_idempotency_key("ariel", "PLTR", "fp-abc", "2026-06-12")
    k2 = dg.discovery_idempotency_key("ariel", "PLTR", "fp-abc", "2026-06-12")
    assert k1 == k2
    assert k1 != dg.discovery_idempotency_key("ariel", "PLTR", "fp-XYZ", "2026-06-12")
    assert k1 != dg.discovery_idempotency_key("ariel", "PLTR", "fp-abc", "2026-06-13")


def _stub_pipeline(monkeypatch, grade_payload, *, quorum_fail=False):
    async def fake_open(**kwargs):
        return 4242
    async def fake_close(**kwargs):
        return None
    async def fake_analysts(**kwargs):
        if quorum_fail:
            from argosy.decisions.per_ticker_analysts import InsufficientAnalystQuorum
            raise InsufficientAnalystQuorum(reason="no quorum", succeeded=[], failed=[])
        return _Result([_Report("fundamentals", "solid growth"),
                        _Report("news", "positive catalysts")])
    async def fake_call(self, *, system, user, **kwargs):
        return ModelCall(text=json.dumps(grade_payload), tokens_in=1,
                         tokens_out=1, model="claude-opus-4-7")
    monkeypatch.setattr(dg, "open_decision_run_for_consult", fake_open)
    monkeypatch.setattr(dg, "_close_decision_run", fake_close)
    monkeypatch.setattr(dg, "run_per_ticker_analysts", fake_analysts)
    monkeypatch.setattr(dg.DiscoveryGraderAgent, "_call_model", fake_call)


def test_grade_returns_fleetpick(monkeypatch):
    _stub_pipeline(monkeypatch, {
        "ticker": "PLTR", "conviction": "HIGH", "verdict": "BUY",
        "thesis_md": "Durable AI-platform growth.", "cites": ["fundamentals", "news"]})
    pick = asyncio.run(dg.grade_discovery_ticker("ariel", _cand("PLTR")))
    assert isinstance(pick, FleetPick)
    assert pick.ticker == "PLTR" and pick.verdict == "BUY" and pick.conviction == "HIGH"
    assert "AI-platform" in pick.thesis_md


def test_grade_returns_none_on_quorum_failure(monkeypatch):
    _stub_pipeline(monkeypatch, {}, quorum_fail=True)
    pick = asyncio.run(dg.grade_discovery_ticker("ariel", _cand("ZZZZ")))
    assert pick is None


def test_synthesis_failure_closes_run_blocked(monkeypatch):
    """codex p2 #4: if the synthesis agent raises (after analysts succeed), the
    decision run must be closed 'blocked', not left 'running'."""
    closed: list[tuple[int, str]] = []

    async def fake_open(**kwargs):
        return 7777

    async def fake_close(*, decision_run_id, status):
        closed.append((decision_run_id, status))

    async def fake_analysts(**kwargs):
        return _Result([_Report("fundamentals", "ok")])

    async def boom(self, **kwargs):
        raise RuntimeError("synthesis exploded")

    monkeypatch.setattr(dg, "open_decision_run_for_consult", fake_open)
    monkeypatch.setattr(dg, "_close_decision_run", fake_close)
    monkeypatch.setattr(dg, "run_per_ticker_analysts", fake_analysts)
    monkeypatch.setattr(dg.DiscoveryGraderAgent, "_call_model", boom)

    with pytest.raises(Exception):
        asyncio.run(dg.grade_discovery_ticker("ariel", _cand("PLTR")))
    assert closed and closed[-1][1] == "blocked"
