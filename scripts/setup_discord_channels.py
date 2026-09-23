"""Create explicitly private chat/status channels without deleting old history.

Uses the separately stored advisor bot token. Channel creation needs Manage
Channels; no role administration or Administrator permission is requested.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from argosy.transport.discord_advisor.config import (  # noqa: E402
    config_path,
    load_bot_token,
    load_config,
)


async def setup() -> dict:
    config = load_config()
    if len(config.bindings) != 1:
        raise SystemExit("This one-household setup requires exactly one binding")
    binding = config.bindings[0]
    token = load_bot_token()
    if not token:
        raise SystemExit("Private advisor token missing")
    async with discord.Client(intents=discord.Intents.none()) as client:
        await client.login(token)
        guild = await client.fetch_guild(int(binding.guild_id))
        bot = await guild.fetch_member(client.user.id)
        owner = await guild.fetch_member(int(binding.user_id))
        if not bot.guild_permissions.manage_channels:
            raise SystemExit("Manage Channels permission is required")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            owner: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            bot: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                             send_messages_in_threads=True),
        }
        existing = await guild.fetch_channels()
        channels = {}
        for name, topic in (
            ("argosy-chat", "Private portfolio research desk | Ask naturally. No trade execution through chat."),
            ("argosy-status", "Scheduled jobs, latest results and failures | Quiet updating board. Check its refresh timestamp."),
        ):
            matches = [c for c in existing if c.name == name]
            if len(matches) > 1:
                raise SystemExit(f"Ambiguous existing {name}; refusing duplicate creation")
            if matches:
                channel = matches[0]
            else:
                channel = await guild.create_text_channel(name, topic=topic, overwrites=overwrites,
                                                         reason="Owner-requested private Argosy chat and scheduled-job status")
            if channel.permissions_for(guild.default_role).view_channel:
                raise SystemExit(f"Existing {name} is not private; no binding changed")
            if not all(channel.permissions_for(bot).__getattribute__(p) for p in
                       ("view_channel", "send_messages", "read_message_history")):
                raise SystemExit(f"Bot cannot access {name}; no binding changed")
            if not channel.permissions_for(owner).view_channel:
                raise SystemExit(f"Owner cannot view {name}; no binding changed")
            channels[name] = str(channel.id)
        updated_binding = binding.model_copy(update={
            "channel_id": channels["argosy-chat"], "status_channel_id": channels["argosy-status"],
        })
        updated = config.model_copy(update={"bindings": (updated_binding,)})
        target = config_path()
        backup = target.with_suffix(".before-private-channels.json")
        if not backup.exists():
            backup.write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
        pending = target.with_suffix(".new")
        pending.write_text(updated.model_dump_json(indent=2) + "\n", encoding="utf-8")
        pending.replace(target)
        return {"channels": channels, "previous_channel_preserved": binding.channel_id,
                "private": True, "configuration_updated": True}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(setup()), indent=2))
