"""Async SQLAlchemy engine + session factory for Argosy.

Phase 0: SQLite via aiosqlite. Engine is lazily constructed and cached;
test code can override the URL by calling `init_engine(url)` before
first use, or by clearing `_state` directly.

Phase 6: `get_session(user_id=...)` accepts an optional tenant
parameter. When the env var `ARGOSY_TENANCY=per-tenant` is set, the
session is scoped to that tenant's DB (per
`argosy.tenancy.tenant_db_path`). Phase 1-5 callers omit `user_id` and
get the legacy global engine; this is the default in dev and tests.

Cross-loop safety (Stream E / 2026-08)
--------------------------------------
aiosqlite pooled connections bind futures to the event loop that
checked them out. Sync callers that used ``asyncio.run`` against the
shared pool raised ``Queue is bound to a different event loop``
(1,065 log hits through 2026-08-07).

Per SQLAlchemy's asyncio docs, a shared ``AsyncEngine`` that may be
touched from more than one loop must use ``NullPool`` so no connection
is reused across loops. File-backed SQLite therefore uses ``NullPool``;
``:memory:`` keeps ``StaticPool`` so every checkout sees the same DB.

Sync callers should still prefer
``argosy.adapters.data.async_bridge.run_coro_sync`` over ``asyncio.run``
so work lands on the app/bridge long-lived loop (no loop churn).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

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

    # Pool choice: see module docstring (cross-loop aiosqlite safety).
    engine_kwargs: dict = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        if ":memory:" in url:
            engine_kwargs["poolclass"] = StaticPool
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        else:
            engine_kwargs["poolclass"] = NullPool

    _engine = create_async_engine(url, **engine_kwargs)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    # SQLite WAL + busy_timeout + synchronous=NORMAL — critical under
    # concurrent writers (e.g. the plan_synthesis flow's ThreadPoolExecutor
    # where N analysts each open their own session via W1.C's agent_reports
    # persistence path).
    #
    # WAL lets readers proceed while a writer holds the lock. Writers still
    # serialize, but each one is fast (~50 ms). busy_timeout=60000 (60 s)
    # gives plenty of headroom for the worst case where all 9 phase-1
    # analysts plus phase-2 / phase-4 agents queue against each other.
    # Run #9 hit 11 s wait times with busy_timeout=10000 and lost EVERY
    # W1.C persistence write — bumping to 60 s eliminates that.
    # synchronous=NORMAL (vs the default FULL) skips per-write fsync on the
    # WAL file, dropping per-INSERT latency from ~30 ms to ~3 ms; still
    # durable on app crash, only loses uncommitted txns on OS crash.
    if url.startswith("sqlite") and ":memory:" not in url:
        @event.listens_for(_engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

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
    # Remember the app loop so sync→async bridges can marshal onto it.
    try:
        from argosy.adapters.data.async_bridge import capture_main_loop

        capture_main_loop()
    except Exception:  # noqa: BLE001 — bridge is optional at import time
        pass

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
