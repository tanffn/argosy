import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from argosy.services.chat_advisor.contracts import Citation, OutboxEvent, Principal
from argosy.services.chat_advisor.conversation import _answer_history, _user_history
from argosy.services.chat_advisor.retrieval import RetrievalService
from argosy.services.chat_advisor.store import ChatStore
from argosy.services.inbox.types import InboxFeed, InboxItem, InboxLiveness, SourceRef
from argosy.state.models import Base, User
from argosy.state.research_models import ResearchItem, ResearchSource
from tests.test_discord_advisor_transport import _worker_db


def _full_detail(reader, principal, record_id):
    chunks = []
    offset = 0
    while True:
        result = reader.read(principal, "actions", record_id=record_id, detail_offset=offset)
        if "detail_excerpt" not in result.data:
            return result.data
        chunks.append(result.data["detail_excerpt"])
        offset = result.data["detail_pagination"]["next_offset"]
        if offset is None:
            return json.loads("".join(chunks))


@pytest.mark.asyncio
async def test_history_includes_only_delivered_same_binding_parent_notices(tmp_path):
    engine, sessions = await _worker_db(tmp_path)
    store = ChatStore(sessions, "owner")
    await store.ensure_binding(provider="discord", guild_id="1", channel_id="2", provider_user_id="3")
    other = ChatStore(sessions, "owner")
    await other.ensure_binding(provider="discord", guild_id="1", channel_id="other", provider_user_id="3")
    try:
        for target, key, category, delivered in [
            (store, "daily", "overview.daily", True),
            (store, "unsent", "overview.daily", False),
            (store, "board", "status_board", True),
            (store, "ops", "status.jobs", True),
            (other, "foreign", "overview.daily", True),
        ]:
            row = await target.enqueue_outbox(OutboxEvent(
                key, "v1", category, f"{key}: run a fleet", [Citation("action_proposal", "9", "today")]))
            if delivered:
                await target.mark_outbox_sent(row.id, key)
        history = await store.history("2")
        assert history[0]["message_id"] == "daily"
        assert datetime.fromisoformat(history[0]["observed_at"]).tzinfo is not None
        assert history == [{"question": None, "response": "daily: run a fleet",
                            "citations": [{"record_type": "action_proposal", "record_id": "9",
                                           "as_of": "today", "version": None, "url": None,
                                           "label": None, "category": None}],
                            "message_id": "daily", "observed_at": history[0]["observed_at"]}]
        assert await store.history("private-thread") == []
        assert _user_history([{"role": "assistant", "content": history[0]["response"]}]) == ""
        restarted = ChatStore(sessions, "owner", binding_id=store.binding_id)
        assert await restarted.history("2") == history
        secret = await store.enqueue_outbox(OutboxEvent("secret", "v1", "overview.daily",
            "account_id: U12345678 token=hidden-secret C:/private/file.txt",
            [Citation("document", "safe", "now", url="C:/private/file.txt")]))
        await store.mark_outbox_sent(secret.id, "redacted-delivery")
        redacted = (await store.history("2"))[-1]
        context = _answer_history([{"role": "assistant", "content": redacted["response"],
                                    "citations": redacted["citations"]}])
        assert "U12345678" not in context and "hidden-secret" not in context
        assert "C:/private/file.txt" not in context
        foreign = ChatStore(sessions, "other", binding_id=store.binding_id)
        assert await foreign.history("2") == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_transport_passes_proactive_context_as_assistant_not_user(tmp_path):
    from argosy.services.chat_advisor.contracts import ChatAnswer
    from argosy.transport.discord_advisor.auth import InboundIdentity, authorize
    from argosy.transport.discord_advisor.gateway import FakeGateway, InboundMessage
    from argosy.transport.discord_advisor.worker import DiscordAdvisorWorker
    from tests.test_discord_advisor_transport import _config, _Dispatcher

    engine, sessions = await _worker_db(tmp_path)
    captured = []

    class Conversation:
        def __init__(self, *args):
            pass

        async def answer(self, *args, history=(), **kwargs):
            captured.extend(history)
            return ChatAnswer("Saved actions, not research.")

    worker = DiscordAdvisorWorker(_config(), "test", FakeGateway(), session_factory=sessions,
        retrieval_factory=lambda _: None, conversation_factory=Conversation,
        dispatcher_factory=_Dispatcher, notification_producer=False)
    try:
        await worker.initialize()
        store = next(iter(worker._stores.values()))
        row = await store.enqueue_outbox(OutboxEvent("overview:today", "v1", "overview.daily", "Your action overview"))
        await store.mark_outbox_sent(row.id, "delivered")
        identity = InboundIdentity("1", "2", "3")
        await worker._handle(authorize(_config(), identity), InboundMessage("new", identity, "Details?"))
        assert captured == [{"role": "assistant", "content": "Your action overview", "citations": [],
                             "message_id": "delivered", "observed_at": captured[0]["observed_at"]}]
        assert _user_history(captured) == ""
    finally:
        await engine.dispose()


def test_research_totals_are_filtered_tenant_scoped_and_independent_of_page(tmp_path):
    path = tmp_path / "research.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([User(id="a"), User(id="b")])
        for owner in ("a", "b"):
            source = ResearchSource(user_id=owner, name=owner, kind="manual", reference=owner)
            db.add(source)
            db.flush()
            for index in range(24):
                db.add(ResearchItem(id=f"{owner}{index:02}", user_id=owner, source_id=source.id,
                                   external_id=str(index), title="Same ticker", url="", author="",
                                   content_hash="hash", body="Evidence" * 400,
                                   observed_at=datetime(2026, 9, 20, tzinfo=UTC),
                                   status="analyzed" if index == 23 else "queued"))
        db.commit()
    reader = RetrievalService(path)
    p = Principal("a", "g", "c", "u")
    try:
        first = reader.read(p, "research", status="queued")
        assert first.data["pagination"] == {"total": 23, "returned": 20, "limit": 20,
                                             "offset": 0, "has_more": True, "next_offset": 20}
        assert first.data["counts_by_status"] == {"queued": 23}
        second = reader.read(p, "research", status="queued", offset=20)
        assert second.data["pagination"]["returned"] == 3
        assert second.data["pagination"]["has_more"] is False
        assert all(row["id"].startswith("a") for row in first.data["items"] + second.data["items"])
        assert len({row["id"] for row in first.data["items"] + second.data["items"]}) == 23
        full = reader.read(p, "research", record_id="a00")
        assert full.data["items"][0]["body"] == "Evidence" * 400
        assert reader.read(p, "research", record_id="b00").data["pagination"]["total"] == 0
        assert reader.read(p, "research", query="absent").data["pagination"]["total"] == 0
    finally:
        reader.engine.dispose()
        engine.dispose()


def test_action_overview_is_compact_but_record_detail_preserves_evidence(tmp_path, monkeypatch):
    items = [InboxItem(id=f"task:{n}", kind="plan_task", title=f"Task {n}", why_now="Required input",
                       due_at="2026-09-30", body={"detail": "Evidence " * 15000, "blockers": ["missing facts"]},
                       source_refs=[SourceRef("plan_action_item", str(n))]) for n in range(3)]
    feed = InboxFeed(items=items, liveness=InboxLiveness("now", 0, 0, True, True),
                     policy_version="test", generated_at="now", trade_plan={"lines": [], "approval_blocked": False,
                     "review_resolution": {"full_evidence": "saved" * 30000}})
    monkeypatch.setattr("argosy.services.inbox.service.build_inbox", lambda *args, **kwargs: feed)
    path = tmp_path / "actions.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    reader = RetrievalService(path)
    p = Principal("a", "g", "c", "u")
    try:
        result = reader.read(p, "inbox")
        assert result.data["pagination"]["total"] == 3
        assert len(json.dumps(result.data)) < 12000
        assert len(result.data["items"]) == 3
        assert result.data["trade_plan"]["line_count"] == 0
        assert result.data["items"][2]["body"]["blockers"] == ["missing facts"]
        assert result.data["items"][2]["summary_omissions"] == ["detail:remainder"]
        full = _full_detail(reader, p, "task:2")
        assert full["items"][0]["body"]["detail"] == items[2].body["detail"]
        assert full["pagination"]["total"] == 1
        assert reader.read(p, "actions", record_id="missing").data["items"] == []
        # Plans from ordinary/cooling proposals have no order_sheet item.
        assert not any(item.kind == "order_sheet" for item in feed.items)
        plan = _full_detail(reader, p, "current_trade_plan")
        assert plan["trade_plan"] == feed.trade_plan
        assert reader.read(p, "actions", record_id="current_trade_plan").citations[0].record_type == "trade_plan"
        assert result.data["trade_plan"]["detail_record_id"] == "current_trade_plan"
    finally:
        reader.engine.dispose()
        engine.dispose()


@pytest.mark.asyncio
async def test_detail_continuation_beyond_old_limit_reaches_answer_model(tmp_path, monkeypatch):
    from argosy.services.chat_advisor.conversation import (
        ConversationService,
        GroundedAnswer,
        RouteDecision,
    )

    feed = InboxFeed(items=[], liveness=InboxLiveness("now", 0, 0, True, True),
        policy_version="test", generated_at="now", trade_plan={"lines": [],
        "review_resolution": {"evidence": "x" * 121000 + "TAIL_EVIDENCE" + "x" * 40000}})
    monkeypatch.setattr("argosy.services.inbox.service.build_inbox", lambda *args, **kwargs: feed)
    path = tmp_path / "continuation.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    reader = RetrievalService(path)
    captured = []

    class Router:
        async def run(self, **kwargs):
            return RouteDecision(intent="read", topic="actions",
                                 filters={"record_id": "current_trade_plan", "detail_offset": 120000})

    class Answer:
        async def run(self, **kwargs):
            captured.append(kwargs)
            return GroundedAnswer(text="Tail evidence reviewed.")

    class Reviewer:
        async def run(self, **kwargs):
            return {"verdict": "accept", "coverage": [
                {"question_part": "Continue the detail", "status": "answered"}]}

    service = ConversationService(reader, dispatcher=None,
        route_agent_factory=lambda _: Router(), answer_agent_factory=lambda _: Answer(),
        review_agent_factory=lambda _: Reviewer())
    try:
        answer = await service.answer(Principal("a", "g", "c", "u"), "Continue the detail", "readonly")
        assert answer.text == "Tail evidence reviewed."
        assert "TAIL_EVIDENCE" in captured[0]["payload"]
        assert "Evidence truncated" not in captured[0]["payload"]
        assert len(captured[0]["payload"]) < 26000
        assert json.loads(captured[0]["payload"])["detail_pagination"]["next_offset"] == 144000
    finally:
        reader.engine.dispose()
        engine.dispose()
