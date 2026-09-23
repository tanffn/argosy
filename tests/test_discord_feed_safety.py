import asyncio
import json
import subprocess
import sys

import discord
import httpx
import pytest
from aiohttp import web
from yarl import URL

from argosy.services.discord_feed_gateway import DiscordFeedGateway
from argosy.services.discord_feed_safety import DiscordFeedSafety, DiscordFeedStopped
from argosy.services.discord_listener import DiscordCreds
from argosy.services.predictions.discord_backfill import _fetch_page


def test_durable_budget_and_auth_stop(tmp_path):
    for _ in range(3):
        DiscordFeedSafety("dummy", directory=tmp_path, clock=lambda: 1000).reserve("identify")
    guard = DiscordFeedSafety("Bot dummy", directory=tmp_path, clock=lambda: 1000)
    guard.clear_auth_block()
    with pytest.raises(DiscordFeedStopped, match="budget"):
        guard.reserve("identify")
    guard.block("repair credentials")
    with pytest.raises(DiscordFeedStopped, match="credentials"):
        DiscordFeedSafety("dummy", directory=tmp_path).check()
    DiscordFeedSafety("different-token", directory=tmp_path).check()
    guard.clear_auth_block()
    guard.check()


def test_single_owner_lease_releases(tmp_path):
    one = DiscordFeedSafety("dummy", directory=tmp_path)
    two = DiscordFeedSafety("dummy", directory=tmp_path)
    with one.lease("gateway"):
        with pytest.raises(DiscordFeedStopped, match="already active"):
            with two.lease("gateway"):
                pytest.fail("duplicate owner")
    with two.lease("gateway"):
        pass


def test_lease_and_budget_across_real_processes(tmp_path):
    script = """
import sys
from pathlib import Path
from argosy.services.discord_feed_safety import DiscordFeedSafety, DiscordFeedStopped
guard = DiscordFeedSafety('dummy', directory=Path(sys.argv[1]))
try:
    with guard.lease('gateway'):
        guard.reserve('identify')
except DiscordFeedStopped as exc:
    print(str(exc))
    sys.exit(7)
"""
    def child():
        return subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    guard = DiscordFeedSafety("dummy", directory=tmp_path)
    with guard.lease("gateway"):
        result = child()
        assert result.returncode == 7, result.stderr
        assert "already active" in result.stdout
    for _ in range(3):
        result = child()
        assert result.returncode == 0, result.stderr
    result = child()
    assert result.returncode == 7, result.stderr
    assert "budget" in result.stdout


@pytest.mark.asyncio
async def test_json_retry_after_and_restart_cooldown(tmp_path):
    clock = [1000.0]
    guard = DiscordFeedSafety("dummy", directory=tmp_path, clock=lambda: clock[0])
    calls = []
    async def sleep(delay):
        calls.append(("sleep", delay))
        clock[0] += delay
    def respond(request):
        calls.append(("request", clock[0]))
        return httpx.Response(429, json={"retry_after": 2.5, "global": True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await _fetch_page(1, None, "dummy", client=client, sleep=sleep, safety=guard)
        with pytest.raises(DiscordFeedStopped, match="rate-limited"):
            await _fetch_page(2, None, "dummy", client=client, safety=guard)
    assert calls == [("request", 1000), ("sleep", 2.5), ("request", 1002.5)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_not_repeated_across_instances(tmp_path, status):
    count = 0
    def respond(request):
        nonlocal count
        count += 1
        return httpx.Response(status, json={"message": "denied"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await _fetch_page(1, None, "dummy", client=client,
                              safety=DiscordFeedSafety("dummy", directory=tmp_path))
        with pytest.raises(DiscordFeedStopped):
            await _fetch_page(1, None, "dummy", client=client,
                              safety=DiscordFeedSafety("dummy", directory=tmp_path))
    assert count == 1


@pytest.mark.asyncio
async def test_long_exhausted_bucket_never_immediately_fetches_next_page(tmp_path):
    guard = DiscordFeedSafety("dummy", directory=tmp_path)
    count = 0
    def respond(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=[], headers={
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "600",
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await _fetch_page(1, None, "dummy", client=client, safety=guard)
        with pytest.raises(DiscordFeedStopped):
            await _fetch_page(1, "123", "dummy", client=client, safety=guard)
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["resume", "resume_storm", "identify_budget", "cancel_handshake", "login_5xx", "auth_stop_during_reidentify", "stalled_construction"])
async def test_real_sdk_local_http_websocket_ready_resume_and_auth_stop(tmp_path, monkeypatch, mode):
    """Actual installed SDK over local TCP HTTP/WS, not an injected gateway fake.

    Only service endpoint URLs are redirected. No Discord credentials or external
    requests: drive HELLO/READY/RECONNECT/RESUMED and an HTTP401 using wire frames.
    """
    counts = {"login": 0, "identify": 0, "resume": 0}
    resumed = asyncio.Event()
    sockets = []
    reject = [False]
    user = {"id": "123", "username": "test-bot", "discriminator": "0001", "avatar": None, "bot": True}
    def response(body, status=200):
        return web.Response(body=json.dumps(body).encode(), content_type="application/json", status=status)
    async def login(request):
        counts["login"] += 1
        if mode == "login_5xx":
            return response({"message": "temporary unavailable", "code": 0}, status=500)
        return response({"message": "Unauthorized", "code": 0}, status=401) if reject[0] else response(user)
    async def app_info(request):
        return response({"id": "456", "name": "fixture", "description": "", "icon": None,
                                  "bot_public": False, "bot_require_code_grant": False,
                                  "owner": user, "verify_key": "test"})
    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.append(ws)
        if mode == "stalled_construction" and len(sockets) == 2:
            async for _ in ws:
                pass
            return ws
        await ws.send_json({"op": 10, "d": {"heartbeat_interval": 1000}})
        if mode == "auth_stop_during_reidentify" and len(sockets) == 2:
            await asyncio.sleep(0.1)
            feed.safety.block("access revoked during IDENTIFY pacing")
        async for message in ws:
            if message.type != web.WSMsgType.TEXT:
                continue
            packet = json.loads(message.data)
            if packet["op"] == 1:
                await ws.send_json({"op": 11, "d": None})
            elif packet["op"] == 2:
                counts["identify"] += 1
                await ws.send_json({"op": 0, "t": "READY", "s": 1, "d": {
                    "v": 10, "user": user, "guilds": [], "session_id": "local-session",
                    "resume_gateway_url": ws_url, "application": {"id": "456", "flags": 0},
                }})
                await asyncio.sleep(0.1)
                await ws.send_json({"op": 9, "d": False} if mode == "auth_stop_during_reidentify" else {"op": 7, "d": None})
            elif packet["op"] == 6:
                counts["resume"] += 1
                assert packet["d"]["session_id"] == "local-session"
                await ws.send_json({"op": 0, "t": "RESUMED", "s": 2, "d": {}})
                resumed.set()
                if mode == "resume_storm":
                    await ws.send_json({"op": 7, "d": None})
        return ws
    app = web.Application()
    app.router.add_get("/users/@me", login)
    app.router.add_get("/oauth2/applications/@me", app_info)
    app.router.add_get("/gateway", socket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    ws_url = f"ws://127.0.0.1:{port}/gateway"
    monkeypatch.setattr(discord.http.Route, "BASE", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(discord.gateway.DiscordWebSocket, "DEFAULT_GATEWAY", URL(ws_url))
    if mode == "stalled_construction":
        wait_for = asyncio.wait_for
        async def fast_construction_timeout(awaitable, timeout):
            return await wait_for(awaitable, 0.5 if timeout == 60.0 and len(sockets) == 1 else timeout)
        # Accelerate only the SDK's construction timeout, not its wire protocol.
        monkeypatch.setattr(asyncio, "wait_for", fast_construction_timeout)
        monkeypatch.setattr(discord.client.ExponentialBackoff, "delay", lambda self: 0)
    creds = DiscordCreds("fixture-token", 1, 2)
    feed = DiscordFeedGateway(creds, safety=DiscordFeedSafety(creds.bot_token, directory=tmp_path))
    try:
        if mode == "identify_budget":
            for _ in range(3):
                feed.safety.reserve("identify")
        if mode in {"identify_budget", "login_5xx"}:
            with pytest.raises(DiscordFeedStopped, match="budget"):
                await asyncio.wait_for(feed.connect(), 20)
            await feed.close()
            if mode == "identify_budget":
                assert counts["identify"] == 0
                assert feed.pending_ws._keep_alive is None
                assert feed.pending_ws.socket.closed
            else:
                assert counts["login"] == 3
            return
        if mode == "cancel_handshake":
            connecting = asyncio.create_task(feed.connect())
            async with asyncio.timeout(5):
                while not feed.pending_ws or not feed.pending_ws._keep_alive:
                    await asyncio.sleep(0.01)
            connecting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await connecting
            await feed.close()
            assert feed.pending_ws._keep_alive is None
            assert feed.pending_ws.socket.closed
            assert counts["identify"] == 0
            return
        if mode == "resume_storm":
            with pytest.raises(DiscordFeedStopped, match="gateway safety budget"):
                await asyncio.wait_for(feed.connect(), 15)
                await asyncio.wait_for(feed.runner, 5)
            assert counts == {"login": 1, "identify": 1, "resume": 9}
            return
        if mode == "auth_stop_during_reidentify":
            with pytest.raises(DiscordFeedStopped, match="revoked during"):
                await asyncio.wait_for(feed.connect(), 15)
                await asyncio.wait_for(feed.runner, 10)
            await feed.close()
            assert counts["identify"] == 1
            assert feed.pending_ws._keep_alive is None
            assert feed.pending_ws.socket.closed
            return
        await asyncio.wait_for(feed.connect(), 15)
        await asyncio.wait_for(resumed.wait(), 5)
        assert feed.ready.is_set()
        assert counts == {"login": 1, "identify": 1, "resume": 1}
        if mode == "stalled_construction":
            assert len(sockets) == 3
            assert sockets[1].closed
            return
        await feed.close()
        reject[0] = True
        bad_creds = DiscordCreds("revoked-fixture-token", 1, 2)
        for _ in range(2):
            failed = DiscordFeedGateway(bad_creds, safety=DiscordFeedSafety(bad_creds.bot_token, directory=tmp_path))
            try:
                with pytest.raises(DiscordFeedStopped):
                    await asyncio.wait_for(failed.connect(), 5)
            finally:
                await failed.close()
        assert counts["login"] == 2  # only first revoked-token attempt reaches HTTP
    finally:
        await feed.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_auth_revoked_while_history_retry_sleeps(tmp_path):
    guard = DiscordFeedSafety("dummy", directory=tmp_path)
    count = 0
    async def sleep(delay):
        guard.block("credential revoked elsewhere")
    def respond(request):
        nonlocal count
        count += 1
        return httpx.Response(429, json={"retry_after": 1})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DiscordFeedStopped, match="revoked"):
            await _fetch_page(1, None, "dummy", client=client, sleep=sleep, safety=guard)
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [DiscordFeedStopped("stop"), asyncio.CancelledError()])
async def test_backfill_closes_owned_attachment_client_on_stop(monkeypatch, failure):
    from argosy.services.predictions import discord_backfill as module
    client = httpx.AsyncClient()
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda: client)
    async def stopped(*args):
        raise failure
    with pytest.raises(type(failure)):
        await module.backfill_discord_predictions(None, channel_id=1, bot_token="fixture", page_fetcher=stopped)
    assert client.is_closed
