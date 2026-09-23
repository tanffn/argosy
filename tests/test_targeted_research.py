"""Question routing tests; live provider verification is a separate probe."""
from collections import defaultdict

import pytest

from argosy.agents.base import AgentReport, ConfidenceBand
from argosy.agents.fund_vehicle_analyst import FundVehicleAnalystAgent
from argosy.agents.fundamentals_analyst import FundamentalsAnalystAgent, FundamentalsReport
from argosy.agents.news_analyst import NewsAnalystAgent
from argosy.agents.remediation import RemediationRequest
from argosy.decisions import per_ticker_analysts as pta


def test_primary_retrieval_question_is_not_a_source():
    question = "Verify the fully diluted count and financing terms."
    for agent, inputs in [
        (FundamentalsAnalystAgent(user_id="test"), {"tickers": ["XYZ"], "fundamentals_payload": {}}),
        (NewsAnalystAgent(user_id="test"), {"tickers": ["XYZ"], "news_payload": {}, "research_inputs": {}}),
    ]:
        system, _, sources = agent.build_prompt(**inputs, research_question=question)
        assert question in system
        assert "Search snippets alone do not verify" in system
        assert "A failed fetch is not proof" in system
        assert "untrusted context" in system
        assert "WebFetch" in agent.claude_code_allowed_tools
        assert agent.claude_code_keep_tool_stream_open is True
        assert not any(question in body for _, body in sources)
        if isinstance(agent, FundamentalsAnalystAgent):
            _, user, _ = agent.build_prompt(**inputs, research_question=question)
            assert "never cite a payload source that is absent" in user
            assert "including when no fundamentals payload exists" in user
            assert "null fair-value estimate" in user


def test_fund_can_investigate_missing_issuer_documents():
    agent = FundVehicleAnalystAgent(user_id="test")
    system, _, sources = agent.build_prompt(ticker="XYZ", fund_context={
        "research_question": "Verify current TER and dated holdings for this share class."
    })
    assert agent.claude_code_allowed_tools == ("WebSearch", "WebFetch")
    assert agent.claude_code_keep_tool_stream_open is True
    assert "Verify current TER" in system
    assert "exact primary document URL actually retrieved" in system
    assert "verified in a dated primary document" in system
    assert not any("Verify current TER" in body for _, body in sources)


@pytest.mark.asyncio
async def test_question_survives_empty_feed_and_real_remediation(monkeypatch):
    question = "Verify financing terms from the dated filing."
    calls = defaultdict(list)

    async def gather(**kwargs):
        return dict.fromkeys(["fundamentals", "news", "indicators", "social", "macro", "fx"], {})

    async def run(make_agent, **inputs):
        agent = make_agent()
        role = agent.agent_role
        calls[role].append(inputs)
        first = len(calls[role]) == 1
        output = FundamentalsReport(
            summary="Research", confidence=ConfidenceBand.LOW,
            cited_sources=["https://issuer.example/filing"],
            remediation_requests=[RemediationRequest(
                kind="data_refresh", target_role=role, ticker="XYZ", reason="Missing filing"
            )] if first else [],
        )
        return AgentReport(agent_role=role, user_id="test", model="test", output=output,
            response_text="{}", tokens_in=1, tokens_out=1, cost_usd=0,
            prompt_hash="test", confidence=ConfidenceBand.LOW)

    async def persist(*args):
        return []

    monkeypatch.setattr(pta, "_gather_inputs_for_ticker", gather)
    monkeypatch.setattr(pta, "_run_analyst_reliably", run)
    monkeypatch.setattr(pta, "_persist_reports", persist)
    result = await pta.run_per_ticker_analysts(
        user_id="test", ticker="XYZ", decision_run_id=1, review_context=question)
    assert set(result.succeeded_roles) == {"fundamentals", "news"}
    assert result.unresolved_remediations == []
    for role in ("fundamentals", "news"):
        assert len(calls[role]) == 2
        assert all(item["research_question"] == question for item in calls[role])
