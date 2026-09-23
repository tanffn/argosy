"""Single authorization boundary shared by messages and interactions."""

from __future__ import annotations

from dataclasses import dataclass

from argosy.services.chat_advisor.contracts import Principal

from .config import DiscordAdvisorConfig


@dataclass(frozen=True)
class InboundIdentity:
    guild_id: str | None
    channel_id: str
    user_id: str
    parent_channel_id: str | None = None
    thread_id: str | None = None
    thread_owner_id: str | None = None
    is_bot: bool = False
    is_webhook: bool = False


def authorize(config: DiscordAdvisorConfig, event: InboundIdentity) -> Principal | None:
    """Return a bound principal or silently reject before persistence/model use."""
    if not config.enabled or event.guild_id is None or event.is_bot or event.is_webhook:
        return None
    parent = event.parent_channel_id or event.channel_id
    for binding in config.bindings:
        if (binding.guild_id, binding.channel_id, binding.user_id) != (
            str(event.guild_id),
            str(parent),
            str(event.user_id),
        ):
            continue
        if event.thread_id is not None and str(event.thread_owner_id or "") != binding.user_id:
            return None
        return Principal(
            household_user_id=binding.household_user_id,
            guild_id=binding.guild_id,
            channel_id=binding.channel_id,
            user_id=binding.user_id,
            thread_id=event.thread_id,
        )
    return None


__all__ = ["InboundIdentity", "authorize"]
