"""Async SQLAlchemy engine + session factory for Argosy.

Phase 0: SQLite via aiosqlite. Engine is lazily constructed and cached;
test code can override the URL by calling `init_engine(url)` before
first use, or by clearing `_state` directly.
"""

from __future__ import annotations

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


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Async context manager yielding an `AsyncSession`."""
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
