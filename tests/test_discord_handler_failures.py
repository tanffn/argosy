import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from argosy.services.chat_advisor.contracts import ChatAnswer
from argosy.state.models import Base, User
from argosy.transport.discord_advisor.auth import InboundIdentity
from argosy.transport.discord_advisor.config import DiscordAdvisorConfig, DiscordBindingConfig
from argosy.transport.discord_advisor.gateway import (
    DiscordAdvisorAccessError,
    FakeGateway,
    InboundMessage,
)
from argosy.transport.discord_advisor.worker import DiscordAdvisorWorker


class Dispatcher:
    def __init__(self, store):
        pass

    async def recover(self):
        pass

    async def stop(self):
        pass


class Conversation:
    def __init__(self, *args):
        pass

    async def answer(self, *args, **kwargs):
        return ChatAnswer("private answer")


class BrokenConversation(Conversation):
    async def answer(self, *args, **kwargs):
        raise RuntimeError("secret-internal-detail")


class PublicGateway(FakeGateway):
    async def send(self, *args, **kwargs):
        raise DiscordAdvisorAccessError("channel_not_private", "Privacy changed", "Restore privacy")


@pytest.mark.asyncio
@pytest.mark.parametrize("privacy_failure", [True, False])
async def test_handler_failure_observed_without_another_inbound_message(tmp_path, privacy_failure):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'chat.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(User(id="owner"))
        await session.commit()
    config = DiscordAdvisorConfig(enabled=True, bindings=(DiscordBindingConfig(
        guild_id="1", channel_id="2", user_id="3", household_user_id="owner",
    ),))
    gateway = PublicGateway() if privacy_failure else FakeGateway()
    worker = DiscordAdvisorWorker(config, "test", gateway, session_factory=sessions,
                                  notification_producer=False, retrieval_factory=lambda _: object(),
                                  dispatcher_factory=Dispatcher,
                                  conversation_factory=Conversation if privacy_failure else BrokenConversation)
    task = asyncio.create_task(worker.run())
    try:
        await gateway.inbound.put(InboundMessage("10", InboundIdentity("1", "2", "3"), "Hi"))
        if privacy_failure:
            with pytest.raises(DiscordAdvisorAccessError, match="Privacy changed"):
                await asyncio.wait_for(task, timeout=3)
            assert gateway.sent == []
        else:
            async with asyncio.timeout(3):
                while not gateway.sent or "couldn't finish" not in gateway.sent[0]["text"]:
                    await asyncio.sleep(0.01)
            assert "couldn't finish" in gateway.sent[0]["text"]
            assert "secret-internal-detail" not in gateway.sent[0]["text"]
            assert not task.done()
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await engine.dispose()
