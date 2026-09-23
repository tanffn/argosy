import asyncio

import pytest

from argosy.services.chat_advisor.contracts import ChatAnswer
from argosy.transport.discord_advisor.auth import InboundIdentity, authorize
from argosy.transport.discord_advisor.gateway import FakeGateway, InboundMessage
from argosy.transport.discord_advisor.worker import DiscordAdvisorWorker
from tests.test_discord_advisor_transport import _config, _Dispatcher, _eventually, _worker_db


@pytest.mark.asyncio
async def test_ack_precedes_model_and_one_durable_message_becomes_answer(tmp_path):
    engine, sessions = await _worker_db(tmp_path)
    gateway = FakeGateway()
    release = asyncio.Event()

    class Conversation:
        def __init__(self, *args):
            pass

        async def answer(self, *args, history=(), progress=None):
            assert gateway.reactions == [("10", "⏳")]
            assert len(gateway.sent) == 1
            await progress("Collecting market news…")
            await release.wait()
            await progress("Checking evidence…")
            return ChatAnswer("Three dated headlines.")

    worker = DiscordAdvisorWorker(_config(), "test", gateway, session_factory=sessions,
                                  retrieval_factory=lambda _: None, conversation_factory=Conversation,
                                  dispatcher_factory=_Dispatcher, notification_producer=False)
    task = asyncio.create_task(worker.run())
    try:
        await gateway.inbound.put(InboundMessage("9", InboundIdentity("1", "2", "999"), "ignored"))
        await gateway.inbound.put(InboundMessage("10", InboundIdentity("1", "2", "3"), "news?"))
        await _eventually(lambda: bool(gateway.edits))
        assert gateway.sent[0]["text"] == "Collecting market news…"
        store = worker._stores[("1", "2", "3")]
        queued = await store.queued_turns()
        assert len(queued) == 1
        assert await store.turn_message_ids(queued[0].id) == [gateway.sent[0]["id"]]
        release.set()
        await _eventually(lambda: ("10", "✅") in gateway.reactions)
        assert len(gateway.sent) == 1
        assert gateway.sent[0]["text"] == "Three dated headlines."
        assert [e["text"] for e in gateway.edits] == [
            "Collecting market news…", "Checking evidence…", "Three dated headlines."]
        assert (await store.history("2"))[0]["response"] == "Three dated headlines."
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await engine.dispose()


@pytest.mark.asyncio
async def test_pending_handoff_recovers_in_original_thread_without_duplicate(tmp_path):
    from argosy.services.chat_advisor.store import ChatStore

    engine, sessions = await _worker_db(tmp_path)
    gateway = FakeGateway()
    store = ChatStore(sessions, "owner")
    await store.ensure_binding(provider="discord", guild_id="1", channel_id="2", provider_user_id="3")
    turn, _ = await store.create_turn(inbound_message_id="20", conversation_key="88", question="review TEST")
    message_id = await gateway.send("88", "Working…", nonce="original")
    await store.save_turn_progress_message(turn.id, message_id)
    request = await store.create_request("20", "88", ["TEST"])
    await store.reserve_turn_delivery(turn.id, response="Fleet dispatched")
    await store.update_request(request.id, "completed")
    worker = DiscordAdvisorWorker(
        _config(), "test", gateway, session_factory=sessions,
        retrieval_factory=lambda _: None, dispatcher_factory=_Dispatcher,
        notification_producer=False,
    )
    try:
        await worker.initialize()
        await _eventually(lambda: ("20", "✅") in gateway.reactions)
        await asyncio.gather(*list(worker._analysis_tasks))
        assert len(gateway.sent) == 1
        assert all(edit["channel_id"] == "88" for edit in gateway.edits)
        assert (await store.reserve_final_delivery(request.id))[1] == [message_id]
    finally:
        for task in list(worker._analysis_tasks):
            task.cancel()
        await asyncio.gather(*worker._analysis_tasks, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_retry_gets_own_reply_but_cancel_cannot_steal_original(tmp_path):
    from argosy.services.chat_advisor.store import ChatStore

    engine, sessions = await _worker_db(tmp_path)
    store = ChatStore(sessions, "owner")
    await store.ensure_binding(provider="discord", guild_id="1", channel_id="2", provider_user_id="3")
    try:
        first, _ = await store.create_turn(inbound_message_id="10", conversation_key="2", question="review")
        await store.save_turn_progress_message(first.id, "100")
        request = await store.create_request("10", "2", ["TEST"])
        cancel, _ = await store.create_turn(inbound_message_id="11", conversation_key="2", question="cancel")
        await store.save_turn_progress_message(cancel.id, "101")
        assert not await store.adopt_analysis_message(request.id, cancel.id)
        assert await store.adopt_analysis_message(request.id, first.id)
        assert not await store.adopt_analysis_message(request.id, cancel.id)
        retry = await store.retry_request(request.id)
        retry_turn, _ = await store.create_turn(inbound_message_id="12", conversation_key="2", question="retry")
        await store.save_turn_progress_message(retry_turn.id, "102")
        assert await store.adopt_analysis_message(retry.id, retry_turn.id)
        assert (await store.analysis_origin_turn(retry.id)).inbound_message_id == "12"
        assert (await store.analysis_for_turn(retry_turn.id)).id == retry.id
        assert (await store.analysis_for_turn(first.id)).id == request.id
        assert await store.analysis_for_turn(cancel.id) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("state,emoji", [("completed", "✅"), ("failed", "⚠️"), ("cancelled", "⏹️")])
async def test_fleet_keeps_original_message_and_finishes_only_after_result(tmp_path, state, emoji):
    engine, sessions = await _worker_db(tmp_path)
    gateway = FakeGateway()

    class Conversation:
        def __init__(self, _, dispatcher):
            self.store = dispatcher.store

        async def answer(self, principal, text, message_id, history=(), progress=None):
            request = await self.store.create_request(
                inbound_message_id=message_id, channel_id=principal.channel_id,
                instruments=["TEST"],
            )
            await self.store.update_request(request.id, "running")
            await self.store.append_progress(request.id, None, "news", "started", "Checking sources")
            return ChatAnswer("Reviewing news and the standing recommendation…",
                              analysis_request_id=request.id)

    worker = DiscordAdvisorWorker(
        _config(), "test", gateway, session_factory=sessions,
        retrieval_factory=lambda _: None, conversation_factory=Conversation,
        dispatcher_factory=_Dispatcher, notification_producer=False,
    )
    try:
        await worker.initialize()
        inbound = InboundMessage("10", InboundIdentity("1", "2", "3"), "Does news change the call?")
        await gateway.defer(inbound)
        await worker._handle(authorize(_config(), inbound.identity), inbound)
        await _eventually(lambda: any("news: started" in e["text"] for e in gateway.edits))
        store = worker._stores[("1", "2", "3")]
        request = await store.get_request_by_inbound("10")
        assert request.state == "running"
        assert len(gateway.sent) == 1
        assert ("10", "✅") not in gateway.reactions
        first_id = gateway.sent[0]["id"]
        assert (await store.reserve_progress_delivery(request.id))[1] == first_id

        # Restart the transport watcher; persisted ownership must survive it.
        tasks = list(worker._analysis_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await store.update_request(request.id, state, error="Research unavailable" if state == "failed" else None)
        await worker._watch_analysis(store, request.id, "2")
        final_text = gateway.sent[0]["text"]
        assert len(gateway.sent) == 1
        assert gateway.reactions[-1] == ("10", emoji)
        assert (await store.reserve_final_delivery(request.id))[1] == [first_id]
        assert (await store.reply_context(first_id))["content"] in final_text
        # Restart after delivery must not regress the final reply into progress.
        await worker._watch_analysis(store, request.id, "2")
        assert gateway.sent[0]["text"] == final_text
        assert len(gateway.sent) == 1
    finally:
        for task in list(worker._analysis_tasks):
            task.cancel()
        await asyncio.gather(*worker._analysis_tasks, return_exceptions=True)
        await engine.dispose()
