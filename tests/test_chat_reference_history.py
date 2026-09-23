import json
from copy import deepcopy
from datetime import UTC, datetime, time

import pytest

from argosy.services.chat_advisor.contracts import ChatAnswer, Citation, Principal
from argosy.services.chat_advisor.history import serialize_history
from argosy.services.chat_advisor.notification_policy import delivery_events
from argosy.services.chat_advisor.notifications import _event, material_version
from argosy.services.chat_advisor.store import ChatStore
from argosy.services.inbox.types import InboxItem, SourceRef
from argosy.transport.discord_advisor.gateway import FakeGateway
from argosy.transport.discord_advisor.worker import DiscordAdvisorWorker
from tests.test_discord_advisor_transport import _config, _Dispatcher, _worker_db


def _item():
    return InboxItem(id="note:76", kind="note", title="Review findings", why_now="Review",
        body={"findings": ["Missing evidence"], "amount": 1000, "blockers": ["b", "a"],
              "generated_at": "yesterday", "quote": {"current_price": 10, "as_of": "yesterday"}},
        source_refs=[SourceRef("action_proposal", "76")])


def test_semantic_versions_change_with_meaning_not_observation_churn():
    original = _item()
    changed = deepcopy(original)
    changed.body["generated_at"] = "today"
    changed.body["quote"] = {"current_price": 20, "as_of": "today"}
    changed.body["blockers"].reverse()
    assert material_version(original) == material_version(changed)
    for field, value in [("title", "Other findings"), ("kind", "trade"), ("due_at", "2030-01-01")]:
        changed = deepcopy(original)
        setattr(changed, field, value)
        assert material_version(changed) != material_version(original)
    for field, value in [("findings", ["Changed evidence"]), ("amount", 2000), ("blockers", [])]:
        changed = deepcopy(original)
        changed.body[field] = value
        assert material_version(changed) != material_version(original)


@pytest.mark.asyncio
async def test_stamped_and_legacy_references_survive_digest_store_restart(tmp_path):
    engine, sessions = await _worker_db(tmp_path)
    store = ChatStore(sessions, "owner")
    await store.ensure_binding(provider="discord", guild_id="1", channel_id="2", provider_user_id="3")
    try:
        item = _item()
        fresh = _event(item, "2026-09-21T09:00:00+00:00")
        # Old JSON without new optional properties remains constructible.
        legacy = Citation(**{"record_type": "action_proposal", "record_id": "229", "as_of": "old"})
        fresh.citations.append(legacy)
        digest = delivery_events([fresh], now=datetime(2026, 9, 21, 12, tzinfo=UTC),
                                 timezone="UTC", overview_time=time(9))[0]
        row = await store.enqueue_outbox(digest)
        await store.mark_outbox_sent(row.id, "daily-message")
        restarted = ChatStore(sessions, "owner", binding_id=store.binding_id)
        history = serialize_history(await restarted.history("2"))
        assert len(history) == 1 and history[0]["role"] == "assistant"
        assert history[0]["message_id"] == "daily-message"
        assert history[0]["observed_at"]
        stamped, old = history[0]["citations"]
        assert (stamped["record_type"], stamped["record_id"]) == ("action_proposal", "76")
        assert (stamped["label"], stamped["category"]) == ("Review findings", "note")
        assert stamped["version"] == material_version(item)
        assert old["record_id"] == "229" and old["label"] is None
        assert await restarted.history("private") == []
        assert await restarted.reply_context("daily-message", conversation_key="private") is None
        context = await restarted.reply_context("daily-message", conversation_key="2")
        assert context["citations"] == history[0]["citations"]
        # Continue through the real conversation seam after the persisted
        # restart; neither model receives a handcrafted reference-hint packet.
        from argosy.services.chat_advisor.conversation import GroundedAnswer, RouteDecision
        from tests.test_chat_read_team import Agent, Reader, review, service, typed

        reader = Reader()
        answerer = Agent(GroundedAnswer(text="Both referenced records checked."))
        reviewer = Agent(review())
        conversation, router = service(reader, RouteDecision(intent="read", reads=[typed("76"), typed("229")]),
                                       answerer, reviewer)
        await conversation.answer(Principal("owner", "1", "2", "3"), "Explain both inbox items", "new-input", history=history)
        groups = json.loads(router.inputs[0]["reference_hints"])
        assert len(groups) == 1 and groups[0]["message_id"] == "daily-message"
        assert groups[0]["observed_at"] == history[0]["observed_at"]
        fresh_hint, legacy_hint = groups[0]["references"]
        assert (fresh_hint["record_type"], fresh_hint["record_id"], fresh_hint["label"], fresh_hint["category"],
                fresh_hint["version"]) == ("action_proposal", "76", "Review findings", "note", material_version(item))
        assert legacy_hint["record_id"] == "229" and legacy_hint["label"] is None
        assert legacy_hint["category"] is None and legacy_hint["version"] is None
        assert reviewer.inputs[0]["hints"] == router.inputs[0]["reference_hints"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_reply_cannot_cross_thread_or_household(tmp_path):
    engine, sessions = await _worker_db(tmp_path)
    store = ChatStore(sessions, "owner")
    await store.ensure_binding(provider="discord", guild_id="1", channel_id="2", provider_user_id="3")
    try:
        turn, _ = await store.create_turn(inbound_message_id="private-input", conversation_key="private",
                                          question="Private question")
        await store.complete_turn(turn.id, response="Private answer")
        await store.mark_turn_delivered(turn.id, ["private-answer"])
        assert await store.reply_context("private-answer") is None
        assert await store.reply_context("private-answer", conversation_key="2") is None
        assert await store.reply_context("private-answer", conversation_key="other-private") is None
        context = await store.reply_context("private-answer", conversation_key="private")
        assert context["content"] == "Private answer"
        assert context["message_id"] == "private-answer"
        foreign = ChatStore(sessions, "other", binding_id=store.binding_id)
        assert await foreign.reply_context("private-answer", conversation_key="private") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_queued_recovery_uses_same_grouped_assistant_notice_history(tmp_path):
    engine, sessions = await _worker_db(tmp_path)
    captured = []

    class Conversation:
        def __init__(self, *args):
            pass

        async def answer(self, principal, text, inbound_id, *, history=()):
            captured.append(history)
            return ChatAnswer("Checked stored references")

    worker = DiscordAdvisorWorker(_config(), "test", FakeGateway(), session_factory=sessions,
        retrieval_factory=lambda _: None, conversation_factory=Conversation,
        dispatcher_factory=_Dispatcher, notification_producer=False)
    try:
        await worker.initialize()
        store = next(iter(worker._stores.values()))
        row = await store.enqueue_outbox(_event(_item(), "now"))
        await store.mark_outbox_sent(row.id, "notice")
        expected = serialize_history(await store.history("2"))
        await store.create_turn(inbound_message_id="interrupted", conversation_key="2", question="Details?")
        await worker._recover_queued(store, Conversation(), Principal("owner", "1", "2", "3"))
        assert captured == [expected]
        assert all(message["role"] == "assistant" for message in captured[0])
        assert captured[0][0]["message_id"] == "notice"
        assert captured[0][0]["citations"][0]["record_id"] == "76"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_digest_origin_thread_inherits_only_its_own_delivered_notice_after_restart(tmp_path):
    engine, sessions = await _worker_db(tmp_path)
    store = ChatStore(sessions, "owner")
    await store.ensure_binding(provider="discord", guild_id="1", channel_id="2", provider_user_id="3")
    try:
        origin = await store.enqueue_outbox(_event(_item(), "first"))
        await store.mark_outbox_sent(origin.id, "origin-message")
        other_item = _item()
        other_item.id, other_item.title = "note:99", "Other parent notice"
        other = await store.enqueue_outbox(_event(other_item, "second"))
        await store.mark_outbox_sent(other.id, "other-message")
        restarted = ChatStore(sessions, "owner", binding_id=store.binding_id)
        history = serialize_history(await restarted.history("origin-message"))
        assert len(history) == 1
        assert history[0]["role"] == "assistant" and history[0]["message_id"] == "origin-message"
        assert history[0]["citations"][0]["record_id"] == "76"
        assert await restarted.history("unrelated-thread") == []
        assert len(await restarted.history("2")) == 2
        context = await restarted.reply_context("origin-message", conversation_key="origin-message")
        assert context and context["message_id"] == "origin-message"
        assert await restarted.reply_context("other-message", conversation_key="origin-message") is None
        assert await restarted.reply_context("origin-message", conversation_key="other-message") is None
        foreign = ChatStore(sessions, "other", binding_id=store.binding_id)
        assert await foreign.history("origin-message") == []
        assert await foreign.reply_context("origin-message", conversation_key="origin-message") is None
    finally:
        await engine.dispose()
