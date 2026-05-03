"""Argosy adapters layer (Phase 2).

Houses outbound integrations: market-data adapters, broker adapters
(Phase 4+), etc. Each adapter raises `MissingAPIKeyError` (or
`MissingDataSourceError`) cleanly when its key is absent so callers can
emit an actionable message rather than crashing on an opaque library
exception.

`MissingAPIKeyError` here subclasses the agents version so a single
`except MissingAPIKeyError` clause in the CLI catches both adapter and
agent missing-key cases.
"""

from __future__ import annotations

from argosy.agents.errors import MissingAPIKeyError as _AgentsMissingAPIKeyError


class MissingAPIKeyError(_AgentsMissingAPIKeyError):
    """Raised when an adapter's API key is not configured.

    Carries an actionable message: which keychain entry / env var to set.
    Inherits from agents.errors.MissingAPIKeyError so CLI handlers that
    catch the agents version also catch adapter errors.
    """

    def __init__(self, *, provider: str, keychain_key: str, env_var: str) -> None:
        message = (
            f"{provider} API key is not configured. Set it via either:\n"
            f"  1. argosy secrets set {keychain_key} <key>\n"
            f"     (stored in the OS keychain; preferred for production)\n"
            f"  2. set {env_var}=<key> in your shell environment\n"
            f"     (transient; convenient for local dev)"
        )
        super().__init__(message)
        self.provider = provider


class MissingDataSourceError(RuntimeError):
    """Raised when an adapter's underlying SDK / package isn't installed."""


__all__ = ["MissingAPIKeyError", "MissingDataSourceError"]
