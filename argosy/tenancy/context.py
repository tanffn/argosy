"""Request-scoped tenant context.

`TenantContext` is a FastAPI dependency that resolves the current
`user_id` from (in order):

  1. A verified NextAuth JWT in the `Authorization: Bearer <token>`
     header (mapped to `user_id` via `users.email`).
  2. The `X-Argosy-User` header (CLI / dev convenience).
  3. The `user_id` query parameter (Phase 1-5 compatibility).

The resolved id is stored in a `ContextVar` for the lifetime of the
request, so downstream helpers can call `current_user_id()` without
plumbing the `Request` object through every layer.

Cross-tenant access is enforced by:

  * Every route that touches DB rows passes the resolved id explicitly
    to the query (Phase 0-5 contract preserved).
  * `require_user_id(claimed)` raises `CrossTenantAccessError` if the
    request context disagrees with `claimed`.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Optional

from fastapi import Header, HTTPException, Query, Request


_USER_ID: ContextVar[Optional[str]] = ContextVar("argosy_user_id", default=None)


class CrossTenantAccessError(RuntimeError):
    """Raised when code attempts to access a different tenant's data."""


def current_user_id() -> str | None:
    """Return the user_id bound to the current request, or None."""
    return _USER_ID.get()


def set_current_user_id(user_id: str | None) -> Token[Optional[str]]:
    """Imperatively bind a user_id; returns a token for `reset()`."""
    return _USER_ID.set(user_id)


@contextmanager
def bind_user_id(user_id: str | None) -> Iterator[None]:
    """Sync context manager binding a user_id to the contextvar."""
    token = _USER_ID.set(user_id)
    try:
        yield
    finally:
        _USER_ID.reset(token)


def require_user_id(claimed: str | None = None) -> str:
    """Return the bound user_id; raise if missing or claim mismatches.

    `claimed` is the id the caller expects (e.g., from a request body).
    If both the contextvar and `claimed` are set and they disagree, this
    raises `CrossTenantAccessError`. If only one is set, that one wins.
    """
    bound = _USER_ID.get()
    if claimed is not None and bound is not None and claimed != bound:
        raise CrossTenantAccessError(
            f"cross-tenant access: bound={bound!r} but claimed={claimed!r}"
        )
    resolved = bound or claimed
    if not resolved:
        raise CrossTenantAccessError("no user_id bound to context")
    return resolved


# ----------------------------------------------------------------------
# FastAPI dependency
# ----------------------------------------------------------------------


def _resolve_from_jwt(authorization: str | None) -> str | None:
    """Verify a Bearer JWT and map its email claim to a user_id.

    Lazy-imported to avoid pulling DB modules into the contextvar hot
    path. Returns None on invalid / absent headers.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    from argosy.api import auth as auth_mod

    claims = auth_mod.verify_jwt(token)
    if claims is None:
        return None
    email = claims.get("email")
    if not email:
        return None
    return auth_mod.user_id_for_email(email)


class TenantContext:
    """FastAPI dependency. Resolves and binds the per-request user_id.

    Usage:
        @router.get("/api/foo")
        async def foo(tenant: TenantContext = Depends()) -> ...:
            user_id = tenant.user_id

    The dependency also binds `user_id` to the contextvar so anything
    downstream can call `current_user_id()` without an extra parameter.
    """

    def __init__(
        self,
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
        x_argosy_user: str | None = Header(default=None, alias="X-Argosy-User"),
        user_id: str | None = Query(default=None),
    ) -> None:
        resolved = (
            _resolve_from_jwt(authorization)
            or x_argosy_user
            or user_id
            or "ariel"  # Phase 1-5 compatibility default
        )
        if not resolved:
            raise HTTPException(status_code=401, detail="no tenant context")
        self.user_id: str = resolved
        # Bind for the lifetime of the request.
        request.state.argosy_user_id = resolved
        # We don't reset() the contextvar here; FastAPI runs each request in
        # its own task with its own ContextVar copy, so the binding does not
        # leak across requests.
        set_current_user_id(resolved)

    def __repr__(self) -> str:  # pragma: no cover
        return f"TenantContext(user_id={self.user_id!r})"


__all__ = [
    "CrossTenantAccessError",
    "TenantContext",
    "bind_user_id",
    "current_user_id",
    "require_user_id",
    "set_current_user_id",
]
