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


# ----------------------------------------------------------------------
# Phase 5: TOTP secret helpers (DB-backed; keychain target later)
# ----------------------------------------------------------------------


def _totp_key(user_id: str) -> str:
    return f"argosy.totp.{user_id}"


def set_totp_secret(user_id: str, secret: str) -> None:
    """Store a user's TOTP secret in the OS keychain (best-effort).

    Phase 5 also writes the secret to the DB via
    `argosy.security.totp.set_user_totp_secret` for read access from the
    API. This keychain call is the productization-ready path; failures
    are silent so dev environments without a keyring still work.
    """
    try:
        set_secret(_totp_key(user_id), secret)
    except KeyringError:  # pragma: no cover - dev fallback
        pass


def get_totp_secret(user_id: str) -> str | None:
    """Return the user's TOTP secret from the keychain, or None."""
    try:
        return get_secret(_totp_key(user_id))
    except KeyringError:  # pragma: no cover
        return None
