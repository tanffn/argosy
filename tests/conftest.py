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


# ---------------------------------------------------------------------------
# Alembic migration test fixtures
# ---------------------------------------------------------------------------
# These fixtures provide a real SQLite database that has been taken through
# the Alembic migration chain so migration tests can verify schema shape.
#
# Isolation pattern: set ARGOSY_HOME to a per-test tmp_path, then call
# reload_settings() to clear the lru_cache.  alembic/env.py re-executes on
# each command.upgrade() call (alembic uses runpy, not Python's import cache)
# and calls get_settings() fresh, picking up the tmp_path DB URL.  The sync
# engine for inspection uses the same path with the aiosqlite driver stripped.
# ---------------------------------------------------------------------------

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine


@pytest.fixture
def alembic_engine_at_head(tmp_path, monkeypatch):
    """A fresh SQLite DB at alembic head, isolated via ARGOSY_HOME."""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings, get_settings
    reload_settings()
    # Settings derives db_file as <ARGOSY_HOME>/db/argosy.db; ensure the dir exists.
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    import os
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    eng = create_engine(sync_url)
    yield eng
    eng.dispose()


@pytest.fixture
def alembic_engine_with_existing_plan_row(tmp_path, monkeypatch):
    """DB upgraded to 0014, a plan_versions row inserted, THEN upgraded to head.

    Verifies backfill of new columns on existing data.
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings, get_settings
    reload_settings()
    # Settings derives db_file as <ARGOSY_HOME>/db/argosy.db; ensure the dir exists.
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    import os
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0014_investor_events_dedup")
    eng = create_engine(sync_url)
    with eng.begin() as conn:
        # Inserts use raw SQL because the fixture operates at the 0014 migration
        # boundary; the SQLAlchemy ORM models reflect post-0015 columns and would
        # fail if used here. Switch to ORM only for fixtures that target head.
        conn.execute(sa.text(
            "INSERT INTO users (id, plan, created_at) VALUES ('ariel', 'free', :now)"
        ), {"now": "2026-01-01"})
        conn.execute(sa.text(
            "INSERT INTO plan_versions (user_id, version_label, source_path, raw_markdown, imported_at) "
            "VALUES ('ariel', 'Jacobs v2.0', '', '# Plan', :now)"
        ), {"now": "2026-02-01"})
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()
