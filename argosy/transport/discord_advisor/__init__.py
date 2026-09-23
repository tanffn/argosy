"""Private Discord advisor transport (disabled until explicitly configured)."""

from .job import build_discord_advisor_job, discord_advisor_metadata

__all__ = ["build_discord_advisor_job", "discord_advisor_metadata"]
