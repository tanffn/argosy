"""Pytest fixtures: in-memory SQLite + FastAPI test client."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from argosy.state import db as db_module
from argosy.state.models import Base


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[None]:
    """Set up an in-memory SQLite engine for the duration of a test."""
    # Each test gets a fresh in-memory DB (shared cache so the connection sees the same DB).
    test_url = "sqlite+aiosqlite:///:memory:"
    eng = db_module.init_engine(test_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield None
    finally:
        await db_module.dispose_engine()


@pytest_asyncio.fixture
async def client(engine: None) -> AsyncIterator[AsyncClient]:
    """FastAPI ASGI test client."""
    # Import lazily so engine fixture has run first.
    from argosy.api.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def project_root() -> str:
    from argosy.config import resolve_home

    return str(resolve_home())
