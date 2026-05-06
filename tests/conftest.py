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
# IMPORTANT: alembic/env.py calls get_settings() at module-execution time and
# overwrites sqlalchemy.url with the production DB path.  We must patch
# get_settings before calling command.upgrade so the temp DB path wins.
# ---------------------------------------------------------------------------

import unittest.mock

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine


def _make_alembic_config(async_db_url: str) -> Config:
    """Build an Alembic Config pointing at async_db_url (sqlite+aiosqlite://)."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", async_db_url)
    return cfg


def _patched_settings(async_db_url: str):
    """Return a mock Settings whose database_url is async_db_url.

    alembic/env.py executes at migration time and does:
        settings = get_settings()
        config.set_main_option("sqlalchemy.url", settings.database_url)

    By patching get_settings we ensure env.py uses our temp DB rather than
    the production DB.  The URL must be the aiosqlite variant because
    env.py calls async_engine_from_config.
    """
    from argosy.config import get_settings

    real = get_settings()
    mock_settings = unittest.mock.MagicMock(wraps=real)
    mock_settings.database_url = async_db_url
    return mock_settings


@pytest.fixture
def alembic_engine_at_head(tmp_path):
    """A fresh SQLite DB upgraded to alembic head."""
    db_path = tmp_path / "argosy_test.db"
    # Alembic env.py needs the async driver; inspection uses the sync driver.
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    cfg = _make_alembic_config(async_url)
    with unittest.mock.patch("argosy.config.get_settings", return_value=_patched_settings(async_url)):
        command.upgrade(cfg, "head")
    eng = create_engine(sync_url)
    yield eng
    eng.dispose()


@pytest.fixture
def alembic_engine_with_existing_plan_row(tmp_path):
    """DB upgraded to 0014, a plan_versions row inserted, THEN upgraded to head.

    Verifies backfill of new columns on existing data.
    """
    db_path = tmp_path / "argosy_test.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    cfg = _make_alembic_config(async_url)
    with unittest.mock.patch("argosy.config.get_settings", return_value=_patched_settings(async_url)):
        command.upgrade(cfg, "0014_investor_events_dedup")
    eng = create_engine(sync_url)
    with eng.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, plan, created_at) VALUES ('ariel', 'free', :now)"
        ), {"now": "2026-01-01"})
        conn.execute(sa.text(
            "INSERT INTO plan_versions (user_id, version_label, source_path, raw_markdown, imported_at) "
            "VALUES ('ariel', 'Jacobs v2.0', '', '# Plan', :now)"
        ), {"now": "2026-02-01"})
    with unittest.mock.patch("argosy.config.get_settings", return_value=_patched_settings(async_url)):
        command.upgrade(cfg, "head")
    yield eng
    eng.dispose()
