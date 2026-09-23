from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from argosy.services.chat_advisor.connection_status import connection_status
from argosy.services.chat_advisor.contracts import ChatAnswer
from argosy.state.models import Base, User
from argosy.transport.discord_advisor.auth import InboundIdentity
from argosy.transport.discord_advisor.config import DiscordAdvisorConfig, DiscordBindingConfig
from argosy.transport.discord_advisor.gateway import (
    DiscordAdvisorAccessError,
    FakeGateway,
    InboundMessage,
)
from argosy.transport.discord_advisor.job import DiscordAdvisorJob
from argosy.transport.discord_advisor.worker import DiscordAdvisorWorker


def _config() -> DiscordAdvisorConfig:
    return DiscordAdvisorConfig(
        enabled=True,
        bindings=(
            DiscordBindingConfig(
                guild_id="1", channel_id="2", user_id="3", household_user_id="owner"
            ),
        ),
    )


class _Dispatcher:
    def __init__(self, _store):
        pass

    async def recover(self):
        return None

    async def stop(self):
        return None


class _Conversation:
    def __init__(self, _retrieval, _dispatcher):
        pass

    async def answer(self, _principal, text, _message_id, history=()):
        return ChatAnswer(f"answer: {text}")


class _RecoveringProducer:
    def __init__(self):
        self.fail = True
        self.calls = 0

    def current_events(self, _principal):
        self.calls += 1
        if self.fail:
            raise RuntimeError("partial feed")
        return []

    def still_current(self, _principal, _event):
        return False


async def _database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notification.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(User(id="owner"))
        await session.commit()
    return engine, sessions


async def _eventually(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_notification_failure_does_not_stop_chat_and_clears_after_retry(tmp_path):
    engine, sessions = await _database(tmp_path)
    producer = _RecoveringProducer()
    gateway = FakeGateway()
    worker = DiscordAdvisorWorker(
        _config(),
        "token",
        gateway,
        session_factory=sessions,
        retrieval_factory=lambda _principal: object(),
        conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher,
        notification_producer=producer,
        notification_retry_seconds=0.02,
    )
    task = asyncio.create_task(worker.run())
    try:
        await _eventually(lambda: worker.notification_error is not None)
        assert not task.done()
        await gateway.inbound.put(
            InboundMessage("1", InboundIdentity("1", "2", "3"), "Hi")
        )
        await _eventually(lambda: any(row["text"] == "answer: Hi" for row in gateway.sent))
        producer.fail = False
        await _eventually(lambda: producer.calls >= 2 and worker.notification_error is None)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await engine.dispose()


@pytest.mark.asyncio
async def test_notification_privacy_failure_remains_terminal(tmp_path):
    engine, sessions = await _database(tmp_path)

    class AccessFailureProducer(_RecoveringProducer):
        def current_events(self, _principal):
            raise DiscordAdvisorAccessError(
                "channel_not_private", "channel became public", "Restore private permissions."
            )

    worker = DiscordAdvisorWorker(
        _config(),
        "token",
        FakeGateway(),
        session_factory=sessions,
        retrieval_factory=lambda _principal: object(),
        conversation_factory=_Conversation,
        dispatcher_factory=_Dispatcher,
        notification_producer=AccessFailureProducer(),
        notification_retry_seconds=0.02,
    )
    with pytest.raises(DiscordAdvisorAccessError, match="channel became public"):
        await asyncio.wait_for(worker.run(), timeout=1)
    await engine.dispose()


def test_job_and_connection_health_expose_notification_degradation(monkeypatch):
    config = _config()
    job = DiscordAdvisorJob(config, "token")
    job._status = "connected"
    job._worker = SimpleNamespace(
        notification_error="Notification feed is temporarily unavailable.",
        status_board_error=None,
    )
    snapshot = job.status_snapshot()
    assert snapshot["connection"] == "connected"
    assert snapshot["notification_error"].startswith("Notification feed")

    monkeypatch.setattr(
        "argosy.transport.discord_advisor.config.load_config", lambda: config
    )
    app_state = SimpleNamespace(
        discord_advisor_startup_error=None,
        discord_advisor_job=job,
    )
    health = connection_status(app_state, user_id="owner")
    assert health == {
        "status": "connected",
        "message": "Notification feed is temporarily unavailable.",
        "attention_required": True,
    }
