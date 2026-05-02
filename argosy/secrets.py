"""Thin wrapper around the OS keychain via the `keyring` library.

All Argosy secrets are scoped under a single service name so they are easy
to find and audit. The actual key names (e.g. anthropic API key) are
configured in `argosy.toml`.
"""

from __future__ import annotations

import keyring
from keyring.errors import KeyringError

SERVICE_NAME = "argosy"


def get_secret(key: str) -> str | None:
    """Return the secret value or None if not set / keyring unavailable."""
    try:
        return keyring.get_password(SERVICE_NAME, key)
    except KeyringError:
        return None


def set_secret(key: str, value: str) -> None:
    """Store a secret under the Argosy service. Raises on backend failure."""
    keyring.set_password(SERVICE_NAME, key, value)


def delete_secret(key: str) -> None:
    """Remove a secret. Best-effort: silently no-op if the entry is absent."""
    try:
        keyring.delete_password(SERVICE_NAME, key)
    except KeyringError:
        pass
