"""Per-tenant database engine factory.

Each tenant gets its own SQLite file at
`${ARGOSY_HOME}/tenants/<user_id>/argosy.db`. Engines are cached so we
don't pay the `create_async_engine()` cost on every request.

Phase 0-5 used a single global engine via `argosy.state.db.get_engine()`.
Phase 6 *adds* the per-tenant factory; the global engine is still the
"control plane" engine, holding the `tenants` registry, `setup_tokens`,
and the `users` table that maps emails -> user_ids. Per-tenant DBs hold
that tenant's positions / proposals / agent reports / fills / audit log.

For Phase 6 we keep the schema identical between control and per-tenant
DBs (both run all migrations); the migrations are tolerant of either
mode. This avoids a fork in the schema definition while still giving us
the isolation we need for the second-tenant gate.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool
from sqlalchemy import event

from argosy.config import get_settings


_LOCK = threading.RLock()
_ENGINES: dict[str, AsyncEngine] = {}
_FACTORIES: dict[str, async_sessionmaker[AsyncSession]] = {}


def tenant_db_path(user_id: str) -> Path:
    """Return the absolute SQLite file path for a tenant's data DB."""
    settings = get_settings()
    safe = "".join(c for c in user_id if c.isalnum() or c in ("-", "_"))
    if not safe:
        raise ValueError(f"invalid user_id for tenant DB path: {user_id!r}")
    return settings.home / "tenants" / safe / "argosy.db"


def _tenant_database_url(user_id: str) -> str:
    path = tenant_db_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _create_tenant_engine(url: str) -> AsyncEngine:
    """Same NullPool / StaticPool rule as ``argosy.state.db`` (Stream E).

    Tenant engines previously used the default AsyncAdaptedQueuePool —
    the exact pool whose loop-bound queue produced the 1,065 production
    cross-loop errors on the global engine.
    """
    kwargs: dict = {"echo": False, "future": True}
    if url.startswith("sqlite"):
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs["poolclass"] = NullPool
    eng = create_async_engine(url, **kwargs)
    if url.startswith("sqlite") and ":memory:" not in url:

        @event.listens_for(eng.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return eng


def get_tenant_engine(user_id: str) -> AsyncEngine:
    """Return (creating if needed) the AsyncEngine for a tenant."""
    with _LOCK:
        eng = _ENGINES.get(user_id)
        if eng is not None:
            return eng
        url = _tenant_database_url(user_id)
        eng = _create_tenant_engine(url)
        _ENGINES[user_id] = eng
        _FACTORIES[user_id] = async_sessionmaker(eng, expire_on_commit=False)
        return eng


def get_tenant_session_factory(
    user_id: str,
) -> async_sessionmaker[AsyncSession]:
    with _LOCK:
        get_tenant_engine(user_id)
        return _FACTORIES[user_id]


@asynccontextmanager
async def get_tenant_session(user_id: str) -> AsyncIterator[AsyncSession]:
    factory = get_tenant_session_factory(user_id)
    async with factory() as session:
        yield session


async def reset_tenant_engines() -> None:
    """Dispose all cached tenant engines. Used in tests + on shutdown."""
    with _LOCK:
        engines = list(_ENGINES.values())
        _ENGINES.clear()
        _FACTORIES.clear()
    for eng in engines:
        try:
            await eng.dispose()
        except Exception:  # pragma: no cover - defensive
            pass


__all__ = [
    "get_tenant_engine",
    "get_tenant_session",
    "get_tenant_session_factory",
    "reset_tenant_engines",
    "tenant_db_path",
]
