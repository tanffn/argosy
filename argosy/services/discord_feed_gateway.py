"""Passive bot gateway: maintained session resume, heartbeat ACKs and backoff."""
from __future__ import annotations

import asyncio
from contextlib import suppress

import aiohttp
import discord

from argosy.services.discord_feed_safety import (
    DiscordFeedSafety,
    DiscordFeedStopped,
    normalize_bot_token,
)


class DiscordFeedGateway:
    def __init__(self, creds, *, safety=None):
        self.creds = creds
        self.safety = safety or DiscordFeedSafety(creds.bot_token)
        self.queue = asyncio.Queue(maxsize=1000)
        self.ready = asyncio.Event()
        self.runner = None
        self.lease = None
        self.pending_ws = None
        self.status_callback = None
        owner = self

        class Client(discord.Client):
            async def before_identify_hook(self, shard_id, *, initial=False):
                if initial:
                    # SDK skips its five-second spacing for initial=True.
                    # A new process is not a new Discord concurrency window.
                    await asyncio.sleep(5)
                # Retain the SDK's reconnect Identify pacing as well as our budgets.
                await super().before_identify_hook(shard_id, initial=initial)
                owner.safety.reserve("identify")

            async def on_ready(self):
                owner.ready.set()
                owner.report_status(True)

            async def on_resumed(self):
                owner.report_status(True)

            async def on_disconnect(self):
                owner.report_status(False)

            async def on_message(self, message):
                if (message.channel.id != creds.channel_id or message.guild is None
                        or message.guild.id != creds.server_id):
                    return
                from argosy.services.discord_attachment_fetcher import parse_attachments
                from argosy.services.discord_listener import MessageEvent
                event = MessageEvent(
                    message_id=str(message.id), channel_id=message.channel.id,
                    content=message.content or "", timestamp=message.created_at,
                    attachments=parse_attachments([a.to_dict() for a in message.attachments]),
                )
                try:
                    owner.queue.put_nowait(event)
                except asyncio.QueueFull:
                    owner.failure = DiscordFeedStopped(
                        "Discord feed queue full; stopped to avoid silent message loss. Backfill required."
                    )
                    await self.close()

        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        trace = aiohttp.TraceConfig()
        async def request_started(session, context, params):
            self.safety.check()
            if params.url.path.endswith("/users/@me"):
                # Counts actual SDK HTTP retries, not just calls to start().
                self.safety.reserve("login_http")
        trace.on_request_start.append(request_started)
        self.client = Client(intents=intents, max_messages=None, http_trace=trace)
        sdk_ws_connect = self.client.http.ws_connect
        async def guarded_ws_connect(*args, **kwargs):
            if self.pending_ws:
                # SDK can internally retry construction after timeout before it
                # assigns Client.ws. Close that abandoned socket BEFORE replacing
                # our reference. Code 4000 preserves a resumable session.
                await self.pending_ws.close(code=4000)
            self.safety.reserve("gateway")
            return await sdk_ws_connect(*args, **kwargs)
        self.client.http.ws_connect = guarded_ws_connect
        # SDK assigns Client.ws only AFTER IDENTIFY. Retain its earlier reference
        # so cancellation/budget rejection during construction also stops heartbeat.
        sdk_update = self.client._connection._update_references
        def track_constructing_socket(ws):
            self.pending_ws = ws
            sdk_update(ws)
        self.client._connection._update_references = track_constructing_socket
        self.failure = None

    def set_status_callback(self, callback):
        self.status_callback = callback

    def report_status(self, connected):
        if self.status_callback:
            self.status_callback(connected)

    async def _run(self):
        try:
            self.safety.reserve("login")
            await self.client.start(normalize_bot_token(self.creds.bot_token), reconnect=True)
        except (discord.LoginFailure, discord.PrivilegedIntentsRequired) as exc:
            reason = f"Discord feed authentication/configuration failed ({type(exc).__name__}); repair credentials or intents before reconnecting."
            self.safety.block(reason)
            raise DiscordFeedStopped(reason) from None
        except discord.ConnectionClosed as exc:
            if exc.code in {4004, 4010, 4011, 4012, 4013, 4014}:
                reason = f"Discord feed gateway rejected authentication/configuration (code {exc.code}); reconnect disabled."
                self.safety.block(reason)
                raise DiscordFeedStopped(reason) from None
            raise
        except discord.HTTPException as exc:
            if exc.status in {401, 403}:
                reason = f"Discord feed HTTP {exc.status}; repair bot access before reconnecting."
                self.safety.block(reason)
                raise DiscordFeedStopped(reason) from None
            raise

    async def connect(self):
        self.lease = self.safety.lease("gateway")
        self.lease.__enter__()
        self.runner = asyncio.create_task(self._run())
        ready_task = asyncio.create_task(self.ready.wait())
        try:
            done, _ = await asyncio.wait(
                [self.runner, ready_task], timeout=90, return_when=asyncio.FIRST_COMPLETED,
            )
            if self.runner in done:
                await self.runner
                if self.failure:
                    raise self.failure
                raise ConnectionError("Discord feed closed before authenticated READY")
            if ready_task not in done:
                raise TimeoutError("Discord feed timed out waiting for authenticated READY")
        finally:
            ready_task.cancel()
            with suppress(asyncio.CancelledError):
                await ready_task

    async def messages(self):
        while True:
            if self.failure:
                raise self.failure
            get_task = asyncio.create_task(self.queue.get())
            try:
                done, _ = await asyncio.wait([get_task, self.runner], return_when=asyncio.FIRST_COMPLETED)
                if self.runner in done:
                    await self.runner
                    if self.failure:
                        raise self.failure
                    return
                yield get_task.result()
            finally:
                get_task.cancel()
                with suppress(asyncio.CancelledError):
                    await get_task

    async def close(self):
        try:
            if self.runner:
                self.runner.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self.runner
            try:
                if self.pending_ws:
                    await self.pending_ws.close(code=1000)
            finally:
                await self.client.close()
        finally:
            if self.lease:
                self.lease.__exit__(None, None, None)
                self.lease = None
