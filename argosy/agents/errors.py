"""Argosy agent errors."""

from __future__ import annotations


class MissingAPIKeyError(RuntimeError):
    """Raised when the Anthropic API key cannot be located.

    Carries an actionable message: how to set it via the keychain or env var.
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or (
                "Anthropic API key is not configured. Set it via either:\n"
                "  1. argosy secrets set argosy.anthropic.api_key <key>\n"
                "     (stored in the OS keychain; preferred for production)\n"
                "  2. export ANTHROPIC_API_KEY=<key> in your shell environment\n"
                "     (transient; convenient for local dev)\n"
                "Then re-run the command."
            )
        )


class AgentRunError(RuntimeError):
    """Raised when an agent run fails for a reason other than a missing key.

    Wraps the underlying exception (Anthropic API error, structured-output
    validation error, etc.) so call sites can handle a single error type.
    """
