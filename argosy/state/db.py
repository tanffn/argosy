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

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from argosy.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

#: Busy-timeout the project applies to EVERY file-backed SQLite connection
#: — async engine and every short-lived sync engine alike. Writers still
#: serialize under WAL, but each waits up to this long for the lock instead
#: of raising ``database is locked`` immediately. Must match the async
#: engine window so sync writers (cache purge, plan routes, the stall-alert
#: loop) don't fail-fast while the async path patiently waits.
SQLITE_BUSY_TIMEOUT_MS = 60_000


def _install_sqlite_pragmas(
    listen_on: object, url: str, *, busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS
) -> None:
    """Attach WAL + busy_timeout + synchronous=NORMAL to a SQLite engine.

    ``listen_on`` is the object the ``connect`` event fires on — the async
    engine's ``.sync_engine`` for the global engine, or a plain sync
    :class:`~sqlalchemy.engine.Engine` for :func:`create_sync_engine`.
    No-op for non-SQLite / ``:memory:`` URLs (the pragmas are irrelevant
    or would break the shared in-memory test DB).
    """
    if not url.startswith("sqlite") or ":memory:" in url:
        return

    @event.listens_for(listen_on, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def create_sync_engine(
    url: str | None = None, *, check_same_thread: bool = False, **kwargs
) -> Engine:
    """Build a synchronous SQLAlchemy engine with the project's SQLite
    reliability pragmas applied (WAL + ``busy_timeout`` + ``synchronous=NORMAL``).

    Many sync code paths (plan/portfolio routes, the synthesis flow, the
    adapter-cache purge, the stall-alert loop) construct a short-lived sync
    engine by stripping the ``+aiosqlite`` driver from the async URL. Built
    with a bare ``sa.create_engine`` those connections get SQLite's default
    ``busy_timeout=0`` and raise ``database is locked`` INSTANTLY on any
    write contention — while the async engine (which sets 60 s) patiently
    waits. This helper is the single seam that keeps every sync engine
    consistent with :func:`init_engine`.

    ``url`` defaults to the settings' database URL with the async driver
    segment stripped.
    """
    if url is None:
        url = get_settings().database_url.replace("+aiosqlite", "")
    connect_args = dict(kwargs.pop("connect_args", {}))
    if url.startswith("sqlite"):
        connect_args.setdefault("check_same_thread", check_same_thread)
    engine = sa.create_engine(url, connect_args=connect_args, **kwargs)
    _install_sqlite_pragmas(engine, url)
    return engine


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
    _install_sqlite_pragmas(_engine.sync_engine, url)

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
