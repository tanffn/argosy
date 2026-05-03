"""Async SQLAlchemy engine + session factory for Argosy.

Phase 0: SQLite via aiosqlite. Engine is lazily constructed and cached;
test code can override the URL by calling `init_engine(url)` before
first use, or by clearing `_state` directly.

Phase 6: `get_session(user_id=...)` accepts an optional tenant
parameter. When the env var `ARGOSY_TENANCY=per-tenant` is set, the
session is scoped to that tenant's DB (per
`argosy.tenancy.tenant_db_path`). Phase 1-5 callers omit `user_id` and
get the legacy global engine; this is the default in dev and tests.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from argosy.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    """Create (or recreate) the engine. Idempotent for the same URL."""
    global _engine, _session_factory
    if url is None:
        settings = get_settings()
        # Make sure the parent dir exists for file-backed SQLite.
        if settings.db_file.parent and not str(settings.db_file).startswith(":memory:"):
            settings.db_file.parent.mkdir(parents=True, exist_ok=True)
        url = settings.database_url
    _engine = create_async_engine(url, echo=echo, future=True)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


def _per_tenant_mode() -> bool:
    return os.environ.get("ARGOSY_TENANCY", "").lower() in ("per-tenant", "tenant")


@asynccontextmanager
async def get_session(user_id: str | None = None) -> AsyncIterator[AsyncSession]:
    """Async context manager yielding an `AsyncSession`.

    Phase 6 routing:
      - When `user_id` is explicitly provided AND `ARGOSY_TENANCY=per-tenant`,
        yields a session bound to that tenant's per-tenant DB.
      - When `user_id` is omitted AND `ARGOSY_TENANCY=per-tenant`, falls
        back to the request-scoped tenant via the TenantContext
        contextvar. This means Phase 0-5 routes that call
        `get_session()` without arguments automatically route to the
        correct tenant DB once a TenantContext is bound on the request.
        If no contextvar is set (e.g., CLI / scheduler outside any
        request), uses the global engine — Phase 1-5 compatibility.
      - Otherwise (no per-tenant mode): always uses the global engine.
    """
    if _per_tenant_mode():
        if user_id is None:
            # Try the request-scoped contextvar before falling back.
            try:
                from argosy.tenancy.context import current_user_id

                user_id = current_user_id()
            except Exception:
                user_id = None
        if user_id:
            from argosy.tenancy.database import get_tenant_session

            async with get_tenant_session(user_id) as session:
                yield session
                return

    factory = get_session_factory()
    async with factory() as session:
        yield session


async def dispose_engine() -> None:
    """Tear down the engine (used in tests / shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    # Also dispose any tenant engines.
    try:
        from argosy.tenancy.database import reset_tenant_engines

        await reset_tenant_engines()
    except Exception:  # pragma: no cover - defensive
        pass
