from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from argosy.transport.discord_advisor.auth import InboundIdentity, authorize
from argosy.transport.discord_advisor.config import DiscordAdvisorConfig, DiscordBindingConfig
from argosy.transport.discord_advisor.gateway import FakeGateway
from argosy.transport.discord_advisor.worker import DiscordAdvisorWorker


def config():
    return DiscordAdvisorConfig(enabled=True, bindings=(DiscordBindingConfig(
        guild_id="1", channel_id="2", status_channel_id="4", user_id="3", household_user_id="owner",
    ),))


def test_status_destination_validated_but_does_not_expand_chat_authority():
    cfg = config()
    worker = DiscordAdvisorWorker(cfg, "test", FakeGateway(), session_factory=lambda: None,
                                 notification_producer=False)
    assert [b.channel_id for b in worker._access_bindings()] == ["2", "4"]
    assert authorize(cfg, InboundIdentity("1", "4", "3")) is None
    assert authorize(cfg, InboundIdentity("1", "2", "3")) is not None


def test_status_destination_must_be_a_snowflake():
    with pytest.raises(ValidationError):
        DiscordBindingConfig(guild_id="1", channel_id="2", status_channel_id="invalid",
                             user_id="3", household_user_id="owner")


@pytest.mark.asyncio
async def test_board_failure_is_visible_without_stopping_chat(monkeypatch):
    import asyncio

    from argosy.transport.discord_advisor import status_board

    cfg = config()
    worker = DiscordAdvisorWorker(cfg, "test", FakeGateway(), session_factory=lambda: None,
                                 notification_producer=False, status_registry=object())
    worker._stores[("1", "2", "3")] = object()
    monkeypatch.setattr(status_board, "publish_status_board", AsyncMock(side_effect=RuntimeError("failed")))
    task = asyncio.create_task(worker._status_board_loop())
    try:
        await asyncio.sleep(0)
        assert worker.status_board_error is not None
        assert "chat remains available" in worker.status_board_error
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
