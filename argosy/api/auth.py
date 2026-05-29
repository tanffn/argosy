"""NextAuth JWT verification (Phase 6, SDD §11.5) + admin-token gate
(Sprint A commit #4 — Spec A §Commit #4, BLOCKER #1).

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
dependencies (other than the optional ``require_admin_token`` helper
below) so it can be imported by the tenancy contextvar resolver
without a circular import.

Admin-token gate
================
``require_admin_token`` is a FastAPI ``Depends`` factory that protects
the mutating ``/api/jobs/*`` routes (Spec A commit #4 — BLOCKER #1).

The gate reads ``settings.admin_token`` (loaded from the
``ARGOSY_ADMIN_TOKEN`` env var by ``argosy.config.get_settings``) and
requires the request to carry a matching ``X-Argosy-Admin`` header.

Note on existing admin endpoints: ``argosy/api/routes/internal.py``
already has a keychain-backed admin check
(``ADMIN_TOKEN_KEY="argosy.admin.token"`` + ``X-Argosy-Admin-Token``
header). That older gate uses a DIFFERENT source (OS keychain,
auto-generated on first request) and a DIFFERENT header name, so we
do NOT reuse it here — the spec explicitly mandates the env-var path
for /api/jobs so the operator can rotate the token without touching
the OS keychain. Codex review focus: keep the two gates distinct;
they cover different threat models (internal/* is single-tenant
admin convenience; /api/jobs guards LLM-cost + DB-mutating triggers).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import Header, HTTPException, status
from sqlalchemy import select

from argosy.config import get_settings
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


class SpecError(Exception):
    """Spec-§8-shaped error that produces a TOP-LEVEL JSON body.

    FastAPI's default ``HTTPException(detail=<dict>)`` wraps the body
    in ``{"detail": {...}}`` — this puts Spec A §8 error shapes
    (``{"error": "admin_token_required"}``, the §1.4 409 conflict
    object, etc.) one nesting level off the wire from what the spec
    documents.

    Routes / dependencies that want spec-shaped error bodies raise
    ``SpecError(status_code, body)``. A FastAPI exception handler
    registered by :func:`argosy.api.routes.jobs.register_routers`
    turns it into a ``JSONResponse`` with ``body`` rendered at the
    top level.

    Distinct from :class:`HTTPException` so this router's handler
    doesn't accidentally intercept other (unrelated) endpoints'
    HTTPExceptions, which keep the default ``{"detail": ...}`` shape.
    """

    def __init__(self, status_code: int, body: dict) -> None:
        super().__init__(f"{status_code}: {body!r}")
        self.status_code = status_code
        self.body = body


async def require_admin_token(
    x_argosy_admin: str | None = Header(default=None, alias="X-Argosy-Admin"),
) -> None:
    """FastAPI dependency: 401 unless ``X-Argosy-Admin`` matches the env token.

    Spec A commit #4, BLOCKER #1. Used by the mutating ``/api/jobs/*``
    routes. Returns ``None`` on success (the request flows through);
    raises :class:`SpecError` (status 401) with a spec-shaped body
    otherwise.

    Behaviour:

    * ``settings.admin_token`` is ``None`` — the dependency raises 401
      ``"admin_token_unconfigured"``. The mutating routes are normally
      refused-to-mount at startup when the env var is unset (see
      ``argosy/api/routes/jobs.py::register_routers``), so this branch
      is a defence-in-depth backstop for the case where the routes
      DID mount (e.g. token set at startup, then cleared at runtime).
    * Missing header → 401 ``"admin_token_required"``.
    * Header present but wrong → 401 ``"admin_token_invalid"``. We use
      ``hmac.compare_digest`` to dodge timing-side-channel inference on
      the (admittedly unlikely) attack surface.
    """
    settings = get_settings()
    expected = settings.admin_token
    if not expected:
        # Defence-in-depth: mutating routes shouldn't be mounted at all
        # when env is unset, but if they were (e.g. a stale process),
        # don't fall through to a no-op gate.
        raise SpecError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            body={"error": "admin_token_unconfigured"},
        )
    if not x_argosy_admin:
        raise SpecError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            body={"error": "admin_token_required"},
        )
    # Constant-time compare to avoid leaking length / prefix via timing.
    if not hmac.compare_digest(x_argosy_admin, expected):
        raise SpecError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            body={"error": "admin_token_invalid"},
        )


__all__ = [
    "SIGNING_KEY_NAME",
    "SpecError",
    "issue_jwt",
    "verify_jwt",
    "user_id_for_email",
    "require_admin_token",
]
