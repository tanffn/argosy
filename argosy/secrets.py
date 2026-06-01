"""Thin wrapper around the OS keychain via the `keyring` library.

All Argosy secrets are scoped under a single service name so they are easy
to find and audit. The actual key names (e.g. anthropic API key) are
configured in `argosy.toml`.

External-service API keys (Finnhub, FRED, etc.) can additionally live in
``~/.argosy/external_api_keys.json`` as a third fallback after keychain
and env-var. This mirrors the Discord-creds convention
(``~/.argosy/discord_creds.json``) so the user has one consistent place
to keep external credentials on disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

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


# ----------------------------------------------------------------------
# External-API-key file (~/.argosy/external_api_keys.json)
# ----------------------------------------------------------------------


def _external_keys_path() -> Path:
    """``~/.argosy/external_api_keys.json`` — expanded via ``os.path.expanduser``
    so it works on POSIX and Windows. Mirrors ``discord_creds.json``."""
    return Path(os.path.expanduser("~")) / ".argosy" / "external_api_keys.json"


def get_external_api_key(provider: str) -> str | None:
    """Read an external-service API key from the on-disk JSON file.

    File shape: a JSON object mapping provider slug to key string, e.g.
    ``{"finnhub": "...", "fred": "..."}``.

    Returns None when the file doesn't exist OR when the requested
    provider isn't in the file — both signal "fall through to the next
    layer in the lookup chain" to the caller, no error.

    Raises ``ValueError`` when the file exists but is malformed (invalid
    JSON, not an object at top level, or the value for ``provider`` is
    empty/non-string). Loud so the user sees the real problem instead of
    a downstream ``MissingAPIKeyError`` pointing at the wrong layer.
    """
    path = _external_keys_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"{path} could not be read: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object at top level")
    if provider not in payload:
        return None
    value = payload[provider]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{path} key {provider!r} must be a non-empty string"
        )
    return value
