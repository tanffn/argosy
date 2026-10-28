"""Live saved-message replay: real Discord parsing/read agents; no sends or actions."""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from argosy.services.chat_advisor.contracts import Principal
from argosy.services.chat_advisor.conversation import ConversationRouterAgent, ConversationService, ResearchAuthorityAgent
from argosy.services.chat_advisor.retrieval import RetrievalService
from argosy.transport.discord_advisor.config import load_config, load_bot_token
from argosy.transport.discord_advisor.gateway import message_payload


class NoActions:
    async def request(self, *args, **kwargs):
        raise AssertionError("Read replay cannot dispatch a fleet")
    request_research = request
    retry = request
    cancel = request


async def main():
    import discord
    binding = load_config().bindings[0]
    principal = Principal(binding.household_user_id, binding.guild_id, binding.channel_id, binding.user_id)
    request = "Try Alpha report import again"
    routed = await ConversationRouterAgent(user_id=principal.household_user_id).run(
        text=request, user_history="", conversation_context="")
    route = routed.output
    assert route.intent == "research_action" and route.research_action == "alpha_capture", route
    authority = await ResearchAuthorityAgent(user_id=principal.household_user_id).run(
        text=request, kind="alpha_capture", reference="configured")
    assert authority.output.authorized
    print(json.dumps({"named_retry_route": route.model_dump(mode="json"), "authority": authority.output.model_dump()}))
    async with discord.Client(intents=discord.Intents.none()) as client:
        await client.login(load_bot_token())
        channel = await client.fetch_channel(int(binding.channel_id))
        forwarded = await channel.fetch_message(1554173427399532625)
        encoded, reply = message_payload(forwarded)
        assert reply is None and "Alpha capture" in encoded
    retrieval = RetrievalService(db_path="db/argosy.db")
    try:
        service = ConversationService(retrieval, NoActions())
        answer = await service.answer(principal, encoded, "preview-forwarded-alert")
        print(json.dumps({"case": "forwarded_alert", "review_status": answer.review_status,
                          "text": answer.text, "review_trace": answer.review_trace}, ensure_ascii=False, default=str))
        assert answer.review_status == "accepted"
        with sqlite3.connect('file:db/argosy.db?mode=ro', uri=True) as db:
            post = db.execute('select question from chat_turns where household_user_id=? and inbound_message_id=?',
                              (principal.household_user_id, '1554198789697445889')).fetchone()[0]
        answer = await service.answer(principal, post, "preview-shared-trade")
        print(json.dumps({"case": "shared_trade", "review_status": answer.review_status,
                          "text": answer.text, "review_trace": answer.review_trace}, ensure_ascii=False, default=str))
        assert answer.review_status == "accepted"
    finally:
        retrieval.engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
