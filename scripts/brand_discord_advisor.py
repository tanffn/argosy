"""Apply existing Argosy branding through an explicit local maintenance command.

Read-only preview by default. No channel/role/privacy changes or bot-chat admin
capability. This is separate from the passive research Discord listener.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from argosy.transport.discord_advisor.config import load_bot_token, load_config  # noqa: E402

DESCRIPTION = (
    "Argosy | Your private portfolio research companion. "
    "Review saved recommendations, ask why, explore tickers with the research fleet, "
    "and follow outcomes. Evidence first. No trade execution through chat."
)
TOPIC = (
    "Your private Argosy desk | Portfolio actions, research & fleet reviews. "
    "Ask naturally; request detail when needed. Quiet alerts. No trades through chat. "
    "Available while the Argosy PC is awake and online."
)


async def brand(*, apply: bool) -> dict:
    config = load_config()
    if len(config.bindings) != 1:
        raise SystemExit("This maintenance command requires exactly one configured binding")
    binding = config.bindings[0]
    logo = (ROOT / "ui" / "public" / "logo.png").read_bytes()
    token = load_bot_token()
    if not token:
        raise SystemExit("Private-advisor bot token missing; run local setup")
    async with discord.Client(intents=discord.Intents.none()) as client:
        await client.login(token)
        app = await client.application_info()
        guild = await client.fetch_guild(int(binding.guild_id))
        channels = await guild.fetch_channels()
        channel = next(c for c in channels if c.id == int(binding.channel_id))
        bot = await guild.fetch_member(client.user.id)
        permissions = channel.permissions_for(bot)
        if channel.permissions_for(guild.default_role).view_channel:
            raise SystemExit("Channel must remain private; no branding changes applied")
        if not (bot.guild_permissions.manage_guild and permissions.manage_channels
                and permissions.view_channel):
            raise SystemExit("Need Manage Server, Manage Channels and private-channel access")
        receipt = {
            "application_id": str(app.id), "guild_id": str(guild.id),
            "channel_id": str(channel.id), "applied": False,
            "changes": ["bot avatar", "application icon and description", "server icon", "channel topic"],
            "server_banner_available": "BANNER" in guild.features,
            "privacy_changed": False,
        }
        if apply:
            await client.user.edit(avatar=logo)
            await app.edit(description=DESCRIPTION, icon=logo,
                           reason="Owner-requested Argosy branding")
            await guild.edit(icon=logo, reason="Owner-requested Argosy branding")
            await channel.edit(topic=TOPIC, reason="Owner-requested Argosy branding")
            updated = await client.fetch_guild(guild.id)
            updated_channel = next(c for c in await updated.fetch_channels() if c.id == channel.id)
            updated_app = await client.application_info()
            receipt["applied"] = True
            receipt["verified"] = bool(
                updated.icon and updated_app.icon
                and updated_app.description == DESCRIPTION and updated_channel.topic == TOPIC
            )
        return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the previewed branding")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(brand(apply=args.apply)), indent=2))
