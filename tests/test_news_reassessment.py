import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from argosy.services.chat_advisor.contracts import AnalysisRequest, Principal, ReadResult
from argosy.services.chat_advisor.conversation import ChatIntent, ConversationService, RouteDecision
from argosy.services.news_reassessment import NewsReassessment, NewsReassessmentAgent


@pytest.mark.asyncio
async def test_compound_question_collects_all_evidence_and_dispatches_research():
    calls = []
    class Router:
        async def run(self, **kwargs):
            return RouteDecision(intent=ChatIntent.ASSESS, instruments=["ABC"], identity_queries=["Alpha"])
    class Reader:
        def read(self, principal, topic, **filters):
            calls.append((topic, filters))
            return ReadResult(topic, [] if topic != "identity" else {"matches": []})
    class Dispatcher:
        async def request(self, principal, instruments, inbound_message_id, *, review_context):
            packet = json.loads(review_context)
            assert [r["topic"] for r in packet["evidence"]] == ["identity", "news", "verdicts", "recommendations"]
            assert packet["ticker"] == "ABC"
            assert "affect" in packet["question"]
            return AnalysisRequest("request", instruments, "queued", prompt_text=review_context)
    service = ConversationService(Reader(), Dispatcher(), lambda _: Router())
    result = await service.answer(Principal("u", "g", "c", "d"), "Does news affect ABC?", "message")
    assert result.analysis_request_id == "request"
    assert calls[0][1]["ticker"] == "ABC"  # alias never erases literal ticker
    assert all(filters == {"ticker": "ABC"} for _, filters in calls[1:])


@pytest.mark.asyncio
async def test_failed_lookup_does_not_dispatch_partial_review():
    class Router:
        async def run(self, **kwargs):
            return RouteDecision(intent=ChatIntent.ASSESS, instruments=["ABC"])
    class Reader:
        def read(self, *args, **kwargs):
            raise RuntimeError("database unavailable")
    class Dispatcher:
        async def request(self, *args, **kwargs):
            pytest.fail("must not claim absent evaluation on failed read")
    result = await ConversationService(Reader(), Dispatcher(), lambda _: Router()).answer(
        Principal("u", "g", "c", "d"), "Does news affect ABC?", "m")
    assert "status is unconfirmed" in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["reuse", "unverified", "review"])
async def test_materiality_agent_controls_reopening_not_legacy_word_overlap(engine, monkeypatch, disposition):
    from argosy.services import news_reassessment, verdict_registry
    from argosy.services.decision_funnel import deep_decision as dd
    standing = SimpleNamespace(id=9, verdict="HOLD", created_at=datetime.now(UTC),
        next_validation=None, reasoning_md="Original thesis", falsifiers_json='["old terms"]',
        revisit_triggers_json="[]", source_decision_run_id=4)
    monkeypatch.setattr(verdict_registry, "check_pushback_gate", lambda *a, **k:
        SimpleNamespace(standing=standing, defended=True))
    async def assess(**kwargs):
        assert kwargs["standing"]["id"] == 9
        assert kwargs["evidence"] == "new facts with entirely different words"
        return NewsReassessment(disposition=disposition, rationale="Agent assessed evidence")
    monkeypatch.setattr(news_reassessment, "assess_news", assess)
    opened = []
    async def open_run(**kwargs):
        opened.append(kwargs)
        raise RuntimeError("test stops at real fleet boundary")
    async def position(**kwargs):
        return ""
    monkeypatch.setattr(dd, "open_decision_run_for_consult", open_run)
    monkeypatch.setattr(dd, "position_context_block", position)
    result = await dd.run_deep_decision(user_id="test", ticker="ABC",
        review_context="new facts with entirely different words", execution_policy="analysis_only")
    if disposition == "review":
        assert opened and opened[0]["execution_policy"] == "analysis_only"
        assert result.blocked_by == "open_error"
    elif disposition == "reuse":
        assert not opened and result.blocked_by == "verdict_defended"
        assert result.news_assessment["disposition"] == "reuse"
    else:
        assert not opened and result.status == "error"
        assert result.blocked_by == "news_evidence_unverified"


def test_agent_searches_current_evidence_not_memory_and_cannot_trade():
    agent = NewsReassessmentAgent(user_id="test")
    system, user, sources = agent.build_prompt(ticker="ABC", standing=None, evidence="untrusted headline")
    assert agent.claude_code_allowed_tools == ("WebSearch",)
    assert "absence alone" in system
    assert "NOT a buy/sell verdict" in system
    assert "untrusted" in system
    assert sources[0][1] == "null"
    assert "UTC" in user


@pytest.mark.asyncio
async def test_review_request_is_research_not_manufactured_news_or_sentiment(monkeypatch):
    from argosy.decisions import per_ticker_analysts as pta
    from argosy.services import research_inputs
    captured = {}
    monkeypatch.setattr(research_inputs, "load_research_inputs", lambda **kwargs: "Existing research")
    async def run(factory, **inputs):
        captured.update(inputs)
    monkeypatch.setattr(pta, "_run_analyst_reliably", run)
    empty_news = {}
    await pta._run_news("u", ["ABC"], empty_news, review_context="Does event change thesis?")
    assert captured["news_payload"] == {}
    assert captured["research_question"] == "Does event change thesis?"
    # Request is separate from citable evidence; the real agent still loads the ledger.
    agent = pta.NewsAnalystAgent(user_id="u")
    system, _, sources = agent.build_prompt(**captured)
    assert "Does event change thesis?" in system
    assert any("Existing research" in body for _, body in sources)
    assert not any("Does event change thesis?" in body for _, body in sources)
    assert pta._news_to_social_payload(empty_news) == {}


@pytest.mark.asyncio
async def test_reuse_requires_independent_review_with_no_first_conclusion(monkeypatch):
    from argosy.services import news_reassessment as nr
    received = []
    async def first(self, **kwargs):
        return SimpleNamespace(output=NewsReassessment(disposition="reuse", rationale="first conclusion"))
    async def second(self, **kwargs):
        received.append(kwargs)
        return SimpleNamespace(output=NewsReassessment(disposition="review", rationale="Dated review overdue"))
    monkeypatch.setattr(nr.NewsReassessmentAgent, "run", first)
    monkeypatch.setattr(nr.NewsReassessmentReviewer, "run", second)
    result = await nr.assess_news(user_id="u", ticker="ABC", standing={"next_validation": "2020-01-01"}, evidence="facts")
    assert result.disposition == "review"
    assert "first conclusion" not in str(received)


@pytest.mark.asyncio
@pytest.mark.parametrize("matched", [0, 1, 2])
async def test_compound_read_never_silently_skips_verdicts(matched):
    from argosy.services.chat_advisor.conversation import GroundedAnswer
    inputs = {}
    topics = []
    calls = []
    class Router:
        async def run(self, **kwargs):
            return RouteDecision(intent=ChatIntent.READ, topic="news", identity_queries=["Alpha"], filters={"limit": 7},
                                 related_topics=["verdicts", "recommendations"])
    class Reader:
        def read(self, principal, topic, **filters):
            topics.append(topic)
            calls.append((topic, filters))
            data = {"total_matches": matched, "matches": [{"ticker": "ABC"}]} if topic == "identity" else []
            if topic == "news":
                data = ["abc-news"] if filters.get("ticker") == "ABC" else ["general-news"]
            return ReadResult(topic, data)
    class Answerer:
        async def run(self, **kwargs):
            inputs.update(kwargs)
            return GroundedAnswer(text="Evidence checked.")
    await ConversationService(Reader(), None, lambda _: Router(), lambda _: Answerer()).answer(
        Principal("u", "g", "c", "d"), "News and saved verdict?", "m")
    if matched == 1:
        assert topics[-2:] == ["verdicts", "recommendations"]
        assert ("news", {"ticker": "ABC", "limit": 7}) in calls
        assert json.loads(inputs["payload"])["primary"] == ["abc-news"]
        assert "general-news" not in inputs["payload"]
    else:
        assert "NOT performed" in inputs["warnings"]
        assert "verdicts" not in topics


def test_news_result_is_overview_not_full_falsifier_dump():
    from argosy.services.chat_advisor.presentation import render_analysis_result
    body = render_analysis_result("r", "completed", {"outcomes": [{
        "ticker": "ABC", "status": "blocked", "verdict": "HOLD", "rationale": "No change warranted.",
        "news_assessment": {"summary": "The latest results support the existing thesis.", "rationale": "long" * 2000},
        "falsifiers": ["long" * 2000], "next_validation": "2026-12-01", "decision_run_id": 1,
    }]})
    assert "latest results" in body and "2026-12-01" in body
    assert "longlong" not in body
    assert len(body) < 600


@pytest.mark.asyncio
async def test_compound_read_failure_is_explicit_not_absence():
    from argosy.services.chat_advisor.conversation import GroundedAnswer
    captured = {}
    class Router:
        async def run(self, **kwargs):
            return RouteDecision(intent=ChatIntent.READ, topic="news", filters={"ticker": "ABC"},
                                 related_topics=["verdicts"])
    class Reader:
        def read(self, principal, topic, **filters):
            if topic == "verdicts":
                raise RuntimeError("read failed")
            return ReadResult(topic, [])
    class Answerer:
        async def run(self, **kwargs):
            captured.update(kwargs)
            return GroundedAnswer(text="Verdict lookup failed.")
    answer = await ConversationService(Reader(), None, lambda _: Router(), lambda _: Answerer()).answer(
        Principal("u", "g", "c", "d"), "News and verdict?", "m")
    assert answer.text == "Verdict lookup failed."
    assert "NOT checked" in captured["warnings"]


@pytest.mark.asyncio
async def test_late_scoped_read_failure_never_answers_from_general_news():
    class Router:
        async def run(self, **kwargs):
            return RouteDecision(intent=ChatIntent.READ, topic="news", identity_queries=["Alpha"], related_topics=["verdicts"])
    class Reader:
        def read(self, principal, topic, **filters):
            if topic == "identity":
                return ReadResult(topic, {"total_matches": 1, "matches": [{"ticker": "ABC"}]})
            if filters.get("ticker"):
                raise RuntimeError("scoped lookup unavailable")
            return ReadResult(topic, ["unrelated general headlines"])
    def answerer(_):
        pytest.fail("No answer model may see the unscoped fallback")
    answer = await ConversationService(Reader(), None, lambda _: Router(), answerer).answer(
        Principal("u", "g", "c", "d"), "Alpha news and verdict?", "m")
    assert "ABC news lookup failed" in answer.text
    assert "cannot assess" in answer.text
