"""Explicit, local-only onboarding for the private Discord advisor."""
from __future__ import annotations

import getpass
import json
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True, help="Configure the private advisory bot, not the research listener.")


@app.command("setup")
def setup(
    guild_id: str = typer.Option(..., help="Private Discord server ID."),
    channel_id: str = typer.Option(..., help="Private text channel ID."),
    discord_user_id: str = typer.Option(..., help="Authorized person's Discord user ID."),
    user_id: str = typer.Option(..., help="Existing Argosy household user ID."),
    timezone: str = typer.Option("Asia/Jerusalem"),
    token_prompt: bool = typer.Option(True, "--token-prompt/--no-token-prompt"),
    path: Annotated[Path | None, typer.Option(help="Optional non-secret configuration file path.")] = None,
) -> None:
    """Bind one private identity and store the bot token in the OS keychain."""
    from argosy.secrets import get_secret, set_secret
    from argosy.transport.discord_advisor.config import DiscordAdvisorConfig, config_path

    # Validate everything before touching credentials or configuration.
    try:
        config = DiscordAdvisorConfig.model_validate({
            "enabled": True,
            "bindings": [{
                "guild_id": guild_id, "channel_id": channel_id,
                "user_id": discord_user_id, "household_user_id": user_id,
            }],
            "timezone": timezone,
        })
    except ValueError:
        raise typer.BadParameter("Invalid IDs or timezone. Use numeric Discord IDs and an existing Argosy household ID.") from None
    target = path or config_path()
    if target.exists():
        # This command intentionally supports one identity. Do not silently
        # discard existing multi-person bindings/preferences on a token refresh.
        from argosy.transport.discord_advisor.config import load_config

        previous = load_config(target)
        identity_fields = ("guild_id", "channel_id", "user_id", "household_user_id")
        def identities(bindings):
            return [tuple(getattr(binding, field) for field in identity_fields) for binding in bindings]
        if previous.bindings and identities(previous.bindings) != identities(config.bindings):
            raise typer.BadParameter("An identity binding already exists. Edit the local configuration deliberately instead of replacing it through setup.")
        config = previous.model_copy(update={"enabled": True, "bindings": previous.bindings or config.bindings, "timezone": timezone})
    typer.echo("Discord will receive the financial/research information you ask Argosy to share in this private channel. Keep channel membership private. No trade execution is available.")
    token = getpass.getpass("Bot token (hidden; blank keeps the stored token): ").strip() if token_prompt else ""
    if token:
        set_secret("discord_advisor_bot_token", token)
    elif not get_secret("discord_advisor_bot_token"):
        raise typer.BadParameter("No private-advisor token is stored. Re-run setup with --token-prompt; do not paste it into chat.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".new")
    temporary.write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    typer.echo("Private advisor configured. Restart Argosy; check its homepage connection status before messaging the bot.")


@app.command("status")
def status() -> None:
    """Inspect configuration only. This does not claim a live bot connection."""
    from argosy.transport.discord_advisor.config import load_bot_token, load_config

    config = load_config()
    typer.echo(json.dumps({
        "enabled": config.enabled,
        "bound_identities": len(config.bindings),
        "token_present": bool(load_bot_token()) if config.enabled else False,
        "connection": "Check the running backend's /health/discord-advisor endpoint or homepage.",
    }, indent=2))
