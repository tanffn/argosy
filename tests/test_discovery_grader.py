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


def test_run_receipt_is_exposed_before_research_failure(monkeypatch):
    events = []
    async def opened(**kwargs):
        return 731
    async def failed(**kwargs):
        events.append("research")
        raise RuntimeError("primary fetch failed")
    async def closed(**kwargs):
        events.append((kwargs["decision_run_id"], kwargs["status"]))
    monkeypatch.setattr(dg, "open_decision_run_for_consult", opened)
    monkeypatch.setattr(dg, "run_per_ticker_analysts", failed)
    monkeypatch.setattr(dg, "_close_decision_run", closed)
    with pytest.raises(RuntimeError, match="primary fetch failed"):
        asyncio.run(dg.grade_discovery_ticker("ariel", {"ticker": "XYZ"},
                                             on_open=lambda ident: events.append(ident)))
    assert events == [731, "research", (731, "blocked")]


def test_grader_sees_complete_limitations_outside_report_excerpt():
    reason = 'Unresolved issuer evidence beyond truncated report text'
    _, prompt = dg.DiscoveryGraderAgent(user_id='ariel').build_prompt(
        ticker='PLTR', analyst_reports=[_Report('news', 'x' * 4500 + reason)],
        evidence_limitations={'unresolved_remediations': [{'reason': reason}],
                              'skipped_roles': [('macro', 'empty payload')]},
    )
    assert reason in prompt
    assert 'empty payload' in prompt
    assert 'A completed analyst call does not verify its inputs' in prompt


def test_grader_delivers_full_tail_and_actual_source_receipts():
    from types import SimpleNamespace

    report = SimpleNamespace(id=815, agent_role="fundamentals",
        response_text="x" * 7000 + "MATERIAL_DILUTION_NOTE",
        sources_json='[{"source_id":"public_document:original","content":"inspected primary evidence"}]')
    _, prompt = dg.DiscoveryGraderAgent(user_id="test").build_prompt(ticker="TEST", analyst_reports=[report])
    assert "MATERIAL_DILUTION_NOTE" in prompt
    assert "public_document:original" in prompt
    assert "inspected primary evidence" in prompt
    assert '"report_id": 815' in prompt
    assert '"truncated": false' in prompt


def test_evidence_budget_failure_is_blocked_not_a_watch_verdict(monkeypatch):
    from argosy.services import discovery_evidence

    _stub_pipeline(monkeypatch, {"ticker": "TEST", "conviction": "LOW", "verdict": "WATCH",
                               "thesis_md": "must not be emitted", "cites": []})
    monkeypatch.setattr(discovery_evidence, "MAX_RENDERED_CHARACTERS", 100)
    closed = []
    async def close(**kwargs):
        closed.append(kwargs)
    monkeypatch.setattr(dg, "_close_decision_run", close)
    with pytest.raises(discovery_evidence.DiscoveryEvidenceIncomplete):
        asyncio.run(dg.grade_discovery_ticker("test", {"ticker": "TEST"}))
    assert closed[-1]["status"] == "blocked"


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
    async def fake_persist(report, *, decision_id):
        return 1
    monkeypatch.setattr(dg, "open_decision_run_for_consult", fake_open)
    monkeypatch.setattr(dg, "_close_decision_run", fake_close)
    monkeypatch.setattr(dg, "run_per_ticker_analysts", fake_analysts)
    monkeypatch.setattr(dg.DiscoveryGraderAgent, "_call_model", fake_call)
    monkeypatch.setattr(dg, "persist_agent_report_async", fake_persist)


def test_grade_returns_fleetpick(monkeypatch):
    _stub_pipeline(monkeypatch, {
        "ticker": "PLTR", "conviction": "HIGH", "verdict": "BUY",
        "thesis_md": "Durable AI-platform growth.", "cites": ["fundamentals", "news"]})
    pick = asyncio.run(dg.grade_discovery_ticker("ariel", _cand("PLTR")))
    assert isinstance(pick, FleetPick)
    assert pick.ticker == "PLTR" and pick.verdict == "BUY" and pick.conviction == "HIGH"
    assert "AI-platform" in pick.thesis_md


@pytest.mark.real_seam
def test_real_grader_agent_dispatch_parses_fleet_pick(monkeypatch):
    """Exercise the real agent dispatch; replace only the external LLM call."""
    payload = {
        "ticker": "PLTR",
        "conviction": "HIGH",
        "verdict": "BUY",
        "thesis_md": "$5B today to $50B outcome = plausible 10x path.",
        "cites": ["fundamentals", "news"],
    }

    async def fake_call(self, *, system, user, **kwargs):
        assert "PLTR" in user and "No fixed 5x/10x threshold" in system
        return ModelCall(
            text=json.dumps(payload), tokens_in=10, tokens_out=10, model="test-model"
        )

    monkeypatch.setattr(dg.DiscoveryGraderAgent, "_call_model", fake_call)
    report = asyncio.run(dg.DiscoveryGraderAgent(user_id="ariel").run(
        ticker="PLTR",
        analyst_reports=[_Report("fundamentals", "solid growth")],
    ))
    assert report.output.verdict == "BUY"
    assert report.output.conviction == "HIGH"


def test_grade_returns_none_on_quorum_failure(monkeypatch):
    _stub_pipeline(monkeypatch, {}, quorum_fail=True)
    pick = asyncio.run(dg.grade_discovery_ticker("ariel", _cand("ZZZZ")))
    assert pick is None


def test_followup_question_reaches_evidence_collectors(monkeypatch):
    _stub_pipeline(monkeypatch, {
        "ticker": "PLTR", "conviction": "LOW", "verdict": "WATCH",
        "thesis_md": "Needs primary evidence", "cites": ["news"]})
    captured = {}

    async def analysts(**kwargs):
        captured.update(kwargs)
        return _Result([_Report("news", "Evidence still missing")])

    monkeypatch.setattr(dg, "run_per_ticker_analysts", analysts)
    question = "Verify the dated fully diluted share count in issuer filings."
    asyncio.run(dg.grade_discovery_ticker("ariel", _cand(), research_question=question))
    assert captured["review_context"] == question


def test_scheduled_discovery_requests_primary_research_and_cannot_execute(monkeypatch):
    _stub_pipeline(monkeypatch, {
        "ticker": "XYL", "conviction": "MED", "verdict": "WATCH",
        "thesis_md": "Unresolved valuation", "cites": ["news"]})
    captured = {}

    async def opened(**kwargs):
        captured.update(kwargs)
        return 42

    async def analysts(**kwargs):
        captured.update(kwargs)
        return _Result([_Report("news", "Dated issuer evidence")])

    monkeypatch.setattr(dg, "open_decision_run_for_consult", opened)
    monkeypatch.setattr(dg, "run_per_ticker_analysts", analysts)
    asyncio.run(dg.grade_discovery_ticker("ariel", _cand("XYL")))
    assert captured["execution_policy"] == "analysis_only"
    assert captured["decision_kind"] == "discovery_research"
    assert "primary documents" in captured["review_context"]
    assert "not proof of no catalyst" in captured["review_context"]


def test_synthesis_failure_closes_run_blocked(monkeypatch):
    """codex p2 #4: if the synthesis agent raises (after analysts succeed), the
    decision run must be closed 'blocked', not left 'running'."""
    closed: list[tuple[int, str]] = []

    async def fake_open(**kwargs):
        return 7777

    async def fake_close(*, decision_run_id, status, **kwargs):
        closed.append((decision_run_id, status))

    async def fake_analysts(**kwargs):
        return _Result([_Report("fundamentals", "ok")])

    async def boom(self, **kwargs):
        raise RuntimeError("synthesis exploded")

    monkeypatch.setattr(dg, "open_decision_run_for_consult", fake_open)
    monkeypatch.setattr(dg, "_close_decision_run", fake_close)
    monkeypatch.setattr(dg, "run_per_ticker_analysts", fake_analysts)
    monkeypatch.setattr(dg.DiscoveryGraderAgent, "_call_model", boom)

    with pytest.raises(RuntimeError, match="synthesis exploded"):
        asyncio.run(dg.grade_discovery_ticker("ariel", _cand("PLTR")))
    assert closed and closed[-1][1] == "blocked"


def test_cancelled_research_closes_lineage_without_fabricating_a_verdict(monkeypatch):
    _stub_pipeline(monkeypatch, {})
    closed = []

    async def cancelled(**kwargs):
        raise asyncio.CancelledError()

    async def close(**kwargs):
        closed.append(kwargs)

    monkeypatch.setattr(dg, 'run_per_ticker_analysts', cancelled)
    monkeypatch.setattr(dg, '_close_decision_run', close)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dg.grade_discovery_ticker('ariel', _cand('XYL')))
    assert closed[-1]['status'] == 'cancelled'
    assert 'cancelled' in closed[-1]['reason']
