import asyncio
from types import SimpleNamespace

import discord
import pytest

from argosy.transport.discord_advisor.gateway import DiscordAdvisorAccessError
from argosy.transport.discord_advisor.worker import DiscordAdvisorWorker, _transient_delivery_error


@pytest.mark.parametrize("status, expected", [(503, True), (429, True), (401, False), (403, False), (404, False)])
def test_only_transient_http_failures_retry(status, expected):
    error = discord.HTTPException(SimpleNamespace(status=status, reason="test"), "test")
    assert _transient_delivery_error(error) is expected


@pytest.mark.asyncio
async def test_watcher_retries_without_stopping_research(monkeypatch):
    worker = object.__new__(DiscordAdvisorWorker)
    calls = []
    delays = []

    async def deliver(*args):
        calls.append(args)
        if len(calls) <= 2:
            raise discord.DiscordServerError(SimpleNamespace(status=503, reason="test"), "outage")

    async def pause(seconds):
        delays.append(seconds)

    worker._watch_analysis_once = deliver
    monkeypatch.setattr(asyncio, "sleep", pause)
    await worker._watch_analysis("store", "request", "channel")
    assert len(calls) == 3 and delays == [2.0, 4.0]
    assert all(call == ("store", "request", "channel") for call in calls)


@pytest.mark.asyncio
async def test_privacy_failure_is_never_retried():
    worker = object.__new__(DiscordAdvisorWorker)

    async def deliver(*args):
        raise DiscordAdvisorAccessError("channel_not_private", "Privacy changed", "Restore privacy")

    worker._watch_analysis_once = deliver
    with pytest.raises(DiscordAdvisorAccessError):
        await worker._watch_analysis(None, "request", "channel")


@pytest.mark.asyncio
async def test_shutdown_still_interrupts_delivery_retry():
    worker = object.__new__(DiscordAdvisorWorker)

    async def deliver(*args):
        raise asyncio.CancelledError

    worker._watch_analysis_once = deliver
    with pytest.raises(asyncio.CancelledError):
        await worker._watch_analysis(None, "request", "channel")
