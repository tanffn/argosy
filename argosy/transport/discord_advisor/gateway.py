"""Small Discord gateway seam; production uses discord.py, tests use FakeGateway."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol

from .auth import InboundIdentity


class DiscordAdvisorAccessError(RuntimeError):
    """A non-transient guild/channel privacy or access failure."""

    def __init__(self, code: str, message: str, repair: str) -> None:
        super().__init__(message)
        self.code = code
        self.repair = repair


@dataclass(frozen=True)
class InboundMessage:
    id: str
    identity: InboundIdentity
    content: str
    interaction: Any | None = None
    reply_to_message_id: str | None = None


class Gateway(Protocol):
    async def start(self, token: str) -> None: ...
    async def wait_ready(self) -> None: ...
    async def reconnects(self): ...
    async def defer(self, message: InboundMessage) -> None: ...
    async def finish_reaction(self, channel_id: str, message_id: str) -> None: ...
    async def set_reaction(self, channel_id: str, message_id: str, emoji: str) -> None: ...
    async def validate_access(self, bindings) -> None: ...
    async def close(self) -> None: ...
    async def messages(self): ...
    async def send(self, channel_id: str, text: str, *, nonce: str) -> str: ...
    async def edit(self, channel_id: str, message_id: str, text: str) -> None: ...
    async def fetch_after(
        self, channel_id: str, after_message_id: str | None, limit: int
    ) -> list[InboundMessage]: ...


class FakeGateway:
    def __init__(self) -> None:
        self.inbound: asyncio.Queue[InboundMessage | None] = asyncio.Queue()
        self.sent: list[dict[str, str]] = []
        self.edits: list[dict[str, str]] = []
        self.reactions: list[tuple[str, str]] = []
        self.history: list[InboundMessage] = []
        self.started = False
        self._ready: asyncio.Queue[bool] = asyncio.Queue()
        self._closed = asyncio.Event()
        self.access_error: DiscordAdvisorAccessError | None = None

    async def start(self, token: str) -> None:
        self.started = True
        self._closed.clear()
        await self._ready.put(True)
        await self._closed.wait()

    async def wait_ready(self) -> None:
        await self._ready.get()

    async def reconnects(self):
        while True:
            await self._ready.get()
            yield None

    async def defer(self, message: InboundMessage) -> None:
        self.reactions.append((message.id, "⏳"))

    async def set_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        self.reactions.append((message_id, emoji))

    async def finish_reaction(self, channel_id: str, message_id: str) -> None:
        self.reactions.append((message_id, "✅"))

    async def validate_access(self, bindings) -> None:
        if self.access_error is not None:
            raise self.access_error

    async def close(self) -> None:
        self.started = False
        self._closed.set()
        await self.inbound.put(None)

    async def messages(self):
        while True:
            item = await self.inbound.get()
            if item is None:
                return
            yield item

    async def send(self, channel_id: str, text: str, *, nonce: str) -> str:
        if len(nonce) > 25:
            raise ValueError("Discord nonce exceeds 25 characters")
        if len(text) > 2000:
            raise ValueError("Discord content exceeds 2000 characters")
        prior = next((m for m in self.sent if m["nonce"] == nonce), None)
        if prior:
            return prior["id"]
        row = {
            "id": str(len(self.sent) + 1),
            "channel_id": channel_id,
            "text": text,
            "nonce": nonce,
        }
        self.sent.append(row)
        return row["id"]

    async def edit(self, channel_id: str, message_id: str, text: str) -> None:
        if len(text) > 2000:
            raise ValueError("Discord content exceeds 2000 characters")
        self.edits.append({"channel_id": channel_id, "message_id": message_id, "text": text})
        for row in self.sent:
            if row["id"] == message_id and row["channel_id"] == channel_id:
                row["text"] = text

    async def fetch_after(
        self, channel_id: str, after_message_id: str | None, limit: int
    ) -> list[InboundMessage]:
        rows = [
            m
            for m in self.history
            if m.identity.channel_id == channel_id
            and (after_message_id is None or int(m.id) > int(after_message_id))
        ]
        return rows[:limit]


class DiscordPyGateway:
    """discord.py adapter. nonce= triggers enforce_nonce=True in discord.py 2.7."""

    def __init__(self) -> None:
        import discord

        intents = discord.Intents.none()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        self._queue: asyncio.Queue[InboundMessage | None] = asyncio.Queue()
        self._ready: asyncio.Queue[bool] = asyncio.Queue()

        self._ack_reactions: dict[tuple[str, str], str] = {}

        @self.client.event
        async def on_ready():
            await self._ready.put(True)

        @self.client.event
        async def on_message(message):
            channel = message.channel
            parent = getattr(channel, "parent", None)
            identity = InboundIdentity(
                guild_id=str(message.guild.id) if message.guild else None,
                channel_id=str(channel.id),
                user_id=str(message.author.id),
                parent_channel_id=str(parent.id) if parent else None,
                thread_id=str(channel.id) if parent else None,
                thread_owner_id=str(getattr(channel, "owner_id", "")) if parent else None,
                is_bot=bool(message.author.bot),
                is_webhook=message.webhook_id is not None,
            )
            reply_to = getattr(getattr(message, "reference", None), "message_id", None)
            await self._queue.put(
                InboundMessage(
                    str(message.id),
                    identity,
                    message.content or "",
                    reply_to_message_id=str(reply_to) if reply_to else None,
                )
            )

        @self.client.event
        async def on_interaction(interaction):
            channel = interaction.channel
            parent = getattr(channel, "parent", None)
            user = interaction.user
            identity = InboundIdentity(
                guild_id=str(interaction.guild_id) if interaction.guild_id else None,
                channel_id=str(interaction.channel_id),
                user_id=str(user.id),
                parent_channel_id=str(parent.id) if parent else None,
                thread_id=str(interaction.channel_id) if parent else None,
                thread_owner_id=str(getattr(channel, "owner_id", "")) if parent else None,
                is_bot=bool(getattr(user, "bot", False)),
                is_webhook=False,
            )
            content = json.dumps(interaction.data or {}, ensure_ascii=False)
            await self._queue.put(
                InboundMessage(str(interaction.id), identity, content, interaction)
            )

    async def start(self, token: str) -> None:
        await self.client.start(token, reconnect=True)

    async def wait_ready(self) -> None:
        await self._ready.get()

    async def reconnects(self):
        while True:
            await self._ready.get()
            yield None

    async def defer(self, message: InboundMessage) -> None:
        interaction = message.interaction
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.defer(thinking=True)
        elif interaction is None:
            await self._reaction(message.identity.thread_id or message.identity.channel_id,
                                 message.id, "⏳")

    async def _reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        import discord

        try:
            channel = await self._channel(channel_id)
            self._assert_private_delivery_channel(channel)
            message = channel.get_partial_message(int(message_id))
            await message.add_reaction(emoji)
            key = (channel_id, message_id)
            previous = self._ack_reactions.get(key)
            self._ack_reactions[key] = emoji
            if previous and previous != emoji:
                await message.remove_reaction(previous, self.client.user)
            if emoji == "✅":
                self._ack_reactions.pop(key, None)
        except discord.HTTPException:
            # Optional reaction permissions/network errors must not prevent an answer.
            # The durable progress message remains the acknowledgement fallback.
            pass

    async def finish_reaction(self, channel_id: str, message_id: str) -> None:
        await self._reaction(channel_id, message_id, "✅")

    async def set_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        await self._reaction(channel_id, message_id, emoji)

    async def validate_access(self, bindings) -> None:
        for binding in bindings:
            guild = self.client.get_guild(int(binding.guild_id))
            if guild is None:
                raise DiscordAdvisorAccessError(
                    "bot_not_invited",
                    "The Discord bot is not a member of the configured server.",
                    "Invite the bot to the configured private server, then reconnect the job.",
                )
            channel = guild.get_channel(int(binding.channel_id))
            if channel is None:
                try:
                    channel = await self.client.fetch_channel(int(binding.channel_id))
                except Exception as exc:
                    raise DiscordAdvisorAccessError(
                        "channel_unavailable",
                        "The configured Discord channel is missing or inaccessible.",
                        "Verify the guild/channel IDs and grant the bot access to the private channel.",
                    ) from exc
            if getattr(getattr(channel, "guild", None), "id", None) != guild.id:
                raise DiscordAdvisorAccessError(
                    "channel_guild_mismatch",
                    "The configured channel does not belong to the configured server.",
                    "Correct the guild/channel binding in configs/discord_advisor.json.",
                )
            bot_member = guild.me
            if bot_member is None:
                raise DiscordAdvisorAccessError(
                    "bot_member_unavailable",
                    "Discord did not expose the bot membership for the configured server.",
                    "Re-invite the bot to the server and reconnect the job.",
                )
            bot_permissions = channel.permissions_for(bot_member)
            missing = [
                label
                for attribute, label in (
                    ("view_channel", "View Channel"),
                    ("send_messages", "Send Messages"),
                    ("read_message_history", "Read Message History"),
                )
                if not getattr(bot_permissions, attribute, False)
            ]
            if missing:
                raise DiscordAdvisorAccessError(
                    "bot_permissions",
                    f"The bot lacks required private-channel permissions: {', '.join(missing)}.",
                    "Grant View Channel, Send Messages, and Read Message History in the configured channel.",
                )
            owner = guild.get_member(int(binding.user_id))
            if owner is None:
                try:
                    owner = await guild.fetch_member(int(binding.user_id))
                except Exception as exc:
                    raise DiscordAdvisorAccessError(
                        "owner_unavailable",
                        "The configured owner is not a member of the configured server.",
                        "Join the configured owner account to the server or correct its Discord user ID.",
                    ) from exc
            if not getattr(channel.permissions_for(owner), "view_channel", False):
                raise DiscordAdvisorAccessError(
                    "owner_cannot_view",
                    "The configured owner cannot view the advisor channel.",
                    "Grant the configured owner View Channel permission.",
                )
            if getattr(channel.permissions_for(guild.default_role), "view_channel", False):
                raise DiscordAdvisorAccessError(
                    "channel_not_private",
                    "The configured advisor channel is visible to @everyone.",
                    "Deny @everyone View Channel and explicitly allow only the owner and bot.",
                )

    async def close(self) -> None:
        await self.client.close()
        await self._queue.put(None)

    async def messages(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def _channel(self, channel_id: str):
        channel = self.client.get_channel(int(channel_id))
        return channel or await self.client.fetch_channel(int(channel_id))

    @staticmethod
    def _assert_private_delivery_channel(channel) -> None:
        """Last-moment privacy guard using discord.py's event-updated cache."""
        guild = getattr(channel, "guild", None)
        if guild is None:
            raise DiscordAdvisorAccessError(
                "delivery_not_guild_channel",
                "Discord refused delivery outside a server channel.",
                "Use only the configured private server channel.",
            )
        if getattr(channel.permissions_for(guild.default_role), "view_channel", False):
            raise DiscordAdvisorAccessError(
                "channel_not_private",
                "Discord blocked delivery because the channel is visible to @everyone.",
                "Deny @everyone View Channel before reconnecting the advisor.",
            )

    async def send(self, channel_id: str, text: str, *, nonce: str) -> str:
        import discord

        channel = await self._channel(channel_id)
        self._assert_private_delivery_channel(channel)
        message = await channel.send(
            text, nonce=nonce, allowed_mentions=discord.AllowedMentions.none()
        )
        return str(message.id)

    async def edit(self, channel_id: str, message_id: str, text: str) -> None:
        import discord

        channel = await self._channel(channel_id)
        self._assert_private_delivery_channel(channel)
        message = await channel.fetch_message(int(message_id))
        await message.edit(content=text, allowed_mentions=discord.AllowedMentions.none())

    async def fetch_after(
        self, channel_id: str, after_message_id: str | None, limit: int
    ) -> list[InboundMessage]:
        import discord

        channel = await self._channel(channel_id)
        result: list[InboundMessage] = []
        after = discord.Object(id=int(after_message_id)) if after_message_id else None
        async for message in channel.history(limit=limit, after=after, oldest_first=True):
            parent = getattr(message.channel, "parent", None)
            reply_to = getattr(getattr(message, "reference", None), "message_id", None)
            result.append(
                InboundMessage(
                    str(message.id),
                    InboundIdentity(
                        str(message.guild.id) if message.guild else None,
                        str(message.channel.id),
                        str(message.author.id),
                        str(parent.id) if parent else None,
                        str(message.channel.id) if parent else None,
                        str(getattr(message.channel, "owner_id", "")) if parent else None,
                        bool(message.author.bot),
                        message.webhook_id is not None,
                    ),
                    message.content or "",
                    reply_to_message_id=str(reply_to) if reply_to else None,
                )
            )
        return result


__all__ = [
    "DiscordAdvisorAccessError",
    "DiscordPyGateway",
    "FakeGateway",
    "Gateway",
    "InboundMessage",
]
