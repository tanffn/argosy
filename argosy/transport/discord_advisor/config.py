"""Strict local configuration; the bot token lives only in the OS keyring."""

from __future__ import annotations

import json
from datetime import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argosy.config import get_settings
from argosy.secrets import get_secret

BOT_TOKEN_KEY = "discord_advisor_bot_token"


class DiscordBindingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    guild_id: str
    channel_id: str
    status_channel_id: str | None = None
    user_id: str
    household_user_id: str = Field(min_length=1, max_length=64)

    @field_validator("guild_id", "channel_id", "user_id")
    @classmethod
    def snowflake(cls, value: str) -> str:
        value = str(value)
        if not value.isdigit() or int(value) <= 0:
            raise ValueError("Discord IDs must be positive decimal snowflakes")
        return value

    @field_validator("status_channel_id")
    @classmethod
    def optional_snowflake(cls, value: str | None) -> str | None:
        return cls.snowflake(value) if value is not None else None


class DiscordAdvisorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    application_id: str | None = None
    bindings: tuple[DiscordBindingConfig, ...] = ()
    timezone: str = "Asia/Jerusalem"
    quiet_hours_start: time = time(22, 0)
    quiet_hours_end: time = time(8, 0)
    daily_overview_time: time = time(9, 0)
    urgent_categories: frozenset[str] = frozenset()
    max_instruments_per_request: int = Field(default=3, ge=1, le=3)
    max_concurrent_fleet_runs_per_household: int = Field(default=1, ge=1, le=1)
    catchup_limit: int = Field(default=50, ge=1, le=200)

    @field_validator("application_id")
    @classmethod
    def application_snowflake(cls, value: str | None) -> str | None:
        if value is not None and (not str(value).isdigit() or int(value) <= 0):
            raise ValueError("application_id must be a positive decimal snowflake")
        return str(value) if value is not None else None

    @model_validator(mode="after")
    def configured_when_enabled(self) -> DiscordAdvisorConfig:
        identities = {(b.guild_id, b.channel_id, b.user_id) for b in self.bindings}
        if len(identities) != len(self.bindings):
            raise ValueError("duplicate Discord identity binding")
        if self.enabled and not self.bindings:
            raise ValueError("enabled advisor requires at least one binding")
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValueError("timezone must be an installed IANA timezone") from exc
        return self


def config_path(home: Path | None = None) -> Path:
    base = home or get_settings().home
    return Path(base) / "configs" / "discord_advisor.json"


def load_config(path: Path | None = None) -> DiscordAdvisorConfig:
    target = path or config_path()
    if not target.is_file():
        return DiscordAdvisorConfig()
    raw = json.loads(target.read_text(encoding="utf-8"))
    return DiscordAdvisorConfig.model_validate(raw)


def load_bot_token() -> str | None:
    token = get_secret(BOT_TOKEN_KEY)
    return token.strip() if token and token.strip() else None


__all__ = [
    "BOT_TOKEN_KEY",
    "DiscordAdvisorConfig",
    "DiscordBindingConfig",
    "config_path",
    "load_bot_token",
    "load_config",
]
