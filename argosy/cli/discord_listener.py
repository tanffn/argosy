"""Local setup for the passive research bot, separate from the advisor bot."""
from __future__ import annotations

import getpass
import json
import warnings
from pathlib import Path
from typing import Annotated

import typer

from argosy.services.discord_listener import _default_creds_path, load_creds

app = typer.Typer(help="Configure the passive Discord research listener.", no_args_is_help=True)
SECRET_KEY = "discord_listener_bot_token"


def _snowflake(value: object, label: str) -> int:
    text = str(value)
    if isinstance(value, bool) or not text.isascii() or not text.isdecimal() or not 0 < int(text) < 2**64:
        raise typer.BadParameter(f"{label} must be a positive numeric Discord ID.")
    return int(text)


@app.command()
def setup(
    guild_id: Annotated[str | None, typer.Option(help="Source server ID; prompted if not already saved.")] = None,
    channel_id: Annotated[str | None, typer.Option(help="Source channel ID; prompted if not already saved.")] = None,
    path: Annotated[Path | None, typer.Option(help="Override the listener configuration path.")] = None,
) -> None:
    """Prompt privately for a token; save locally without connecting or enabling."""
    from argosy.secrets import get_secret, set_secret

    target = path or _default_creds_path()
    previous = {}
    if target.exists():
        try:
            previous = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(previous, dict):
                raise ValueError
        except (ValueError, OSError):
            raise typer.BadParameter("Existing listener configuration is unreadable; repair it before setup.") from None
    guild = _snowflake(guild_id or previous.get("server_id") or typer.prompt("Source server ID"), "guild-id")
    channel = _snowflake(channel_id or previous.get("channel_id") or typer.prompt("Alpha Report channel ID"), "channel-id")
    for key, value in (("server_id", guild), ("channel_id", channel)):
        if key in previous and _snowflake(previous[key], key) != value:
            raise typer.BadParameter("Saved source differs. Token refresh will not silently replace its server/channel.")
    # Refuse getpass's visible-input fallback (e.g. a redirected stdin).
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Listener BOT token (hidden; blank keeps existing): ").strip()
    except (getpass.GetPassWarning, EOFError):
        raise typer.BadParameter("Run setup in an interactive terminal with hidden input; never pass a token on the command line.") from None
    token = token or get_secret(SECRET_KEY) or previous.get("bot_token", "")
    if not isinstance(token, str):
        raise typer.BadParameter("No valid listener bot token supplied.")
    token = token.strip().removeprefix("Bot ").strip()
    from argosy.services.discord_listener import _DISCORD_TOKEN_MIN_LEN, _DISCORD_TOKEN_PREFIXES
    if len(token) < _DISCORD_TOKEN_MIN_LEN or not token.startswith(_DISCORD_TOKEN_PREFIXES) or any(c.isspace() for c in token):
        raise typer.BadParameter("Token appears incomplete or malformed. Copy the listener BOT token from the Developer Portal.")
    # Never overwrite the advisor credential or store a token in JSON.
    try:
        set_secret(SECRET_KEY, token)
    except Exception:
        raise typer.BadParameter("Could not save the token in the OS credential manager. Nothing was enabled.") from None
    payload = {**previous, "server_id": guild, "channel_id": channel, "token_secret": SECRET_KEY}
    payload.pop("bot_token", None)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".new")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
    except OSError:
        raise typer.BadParameter("Token saved in the OS credential manager, but configuration could not be saved. Repair the path and rerun setup. Nothing was enabled.") from None
    typer.echo("Listener credentials saved in the OS credential manager. No connection made; feed enablement unchanged.")
    typer.echo(f"Source server: {guild}; channel: {channel}. Ready for a bounded access check and bot branding.")


@app.command()
def status() -> None:
    """Inspect local configuration without contacting Discord or printing tokens."""
    try:
        creds = load_creds()
    except ValueError:
        raise typer.BadParameter("Listener credentials missing or invalid; run discord-listener setup.") from None
    typer.echo(json.dumps({"configured": creds is not None,
                           "server_id": creds.server_id if creds else None,
                           "channel_id": creds.channel_id if creds else None,
                           "live_connection_verified": False}, indent=2))
