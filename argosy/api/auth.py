"""NextAuth JWT verification (Phase 6, SDD §11.5).

NextAuth issues HS256 JWTs whose signing secret we share via the
keychain entry `argosy.nextauth.signing_key`. We verify them with
stdlib HMAC (no PyJWT dependency) and map the email claim to a
user_id via the `users.email` column.

Token format (NextAuth default):

    base64url(header).base64url(payload).base64url(hmac)

  payload = {
    "email": <user's email>,
    "iat": <unix>,
    "exp": <unix>,
    "sub": <NextAuth subject id>,
    ...
  }

This module is intentionally lightweight — it has no FastAPI
dependencies so it can be imported by the tenancy contextvar resolver
without a circular import.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from sqlalchemy import select

from argosy.secrets import get_secret


SIGNING_KEY_NAME = "argosy.nextauth.signing_key"


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _signing_key() -> str | None:
    return get_secret(SIGNING_KEY_NAME)


def issue_jwt(claims: dict[str, Any], *, ttl_seconds: int = 24 * 60 * 60) -> str:
    """Issue an HS256 JWT for testing / first-login flow.

    Production uses NextAuth on the frontend to issue tokens; this
    helper exists for setup-token redemption and tests.
    """
    key = _signing_key()
    if not key:
        raise RuntimeError(
            f"signing key not configured: set keychain entry {SIGNING_KEY_NAME!r}"
        )
    now = int(time.time())
    payload = {**claims, "iat": now, "exp": now + ttl_seconds}
    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(
        key.encode("utf-8"), f"{h}.{p}".encode("ascii"), hashlib.sha256
    ).digest()
    return f"{h}.{p}.{_b64url_encode(sig)}"


def verify_jwt(token: str, *, now: int | None = None) -> dict[str, Any] | None:
    """Verify a JWT signed by the shared NextAuth secret.

    Returns the decoded claims dict on success, or None on any error.
    Never raises.
    """
    if not token or token.count(".") != 2:
        return None
    key = _signing_key()
    if not key:
        return None
    try:
        h_b64, p_b64, s_b64 = token.split(".")
        header = json.loads(_b64url_decode(h_b64))
        if not isinstance(header, dict):
            return None
        if header.get("alg") != "HS256":
            return None
        signing_input = f"{h_b64}.{p_b64}".encode("ascii")
        expected = hmac.new(
            key.encode("utf-8"), signing_input, hashlib.sha256
        ).digest()
        actual = _b64url_decode(s_b64)
        if not hmac.compare_digest(expected, actual):
            return None
        payload = json.loads(_b64url_decode(p_b64))
        if not isinstance(payload, dict):
            return None
        moment = now if now is not None else int(time.time())
        exp = payload.get("exp")
        if isinstance(exp, (int, float)) and exp < moment:
            return None
        return payload
    except (ValueError, json.JSONDecodeError, KeyError, TypeError):
        return None


def user_id_for_email(email: str) -> str | None:
    """Sync lookup helper: returns `users.id` for an email, or None.

    Synchronous so the contextvar resolver can call it without
    materializing an event loop. Uses a one-shot stdlib `sqlite3`
    connection against the control DB to avoid the deadlock that
    `loop.run_until_complete()` would cause inside FastAPI request
    handlers (which are already running an event loop).
    """
    import sqlite3

    from argosy.config import get_settings

    settings = get_settings()
    db_file = settings.db_file
    if not db_file.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_file))
        try:
            row = conn.execute(
                "SELECT id FROM users WHERE email = ? LIMIT 1", (email,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


__all__ = [
    "SIGNING_KEY_NAME",
    "issue_jwt",
    "verify_jwt",
    "user_id_for_email",
]
