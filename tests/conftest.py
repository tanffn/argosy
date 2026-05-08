"""Pytest fixtures: in-memory SQLite + FastAPI test client."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from argosy.state import db as db_module
from argosy.state.models import Base


@pytest.fixture
def client_with_db(tmp_path):
    """Synchronous TestClient backed by a dedicated file-backed SQLite DB.

    Provides ``client_with_db.app.state.session_factory`` so test setup
    code can insert rows directly, and overrides the ``get_db`` dependency
    used by the plan routes.

    Both the sync engine (for the route's get_db dependency and fixture
    setup) and the async engine (for distill_baseline_plan_async which
    opens its own session via db_mod.get_session()) are pointed at the
    same file-backed SQLite so all code paths share the same data.
    """
    import asyncio

    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker
    from starlette.testclient import TestClient

    from argosy.api.main import create_app
    from argosy.api.routes.plan import get_db

    # File-backed SQLite in tmp_path; shared by sync + async connections.
    db_path = tmp_path / "test_plan.db"
    sync_url = f"sqlite:///{db_path}"
    async_url = f"sqlite+aiosqlite:///{db_path}"

    # Sync engine — used by the get_db dependency and by fixture setup.
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    # Async engine — used by distill_baseline_plan_async's db_mod.get_session().
    # Point it at the same file so the async path can read the rows that
    # the sync path inserted.
    db_module.init_engine(async_url)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = create_app()
    app.state.session_factory = SessionLocal
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, raise_server_exceptions=True) as tc:
        yield tc

    # Tear down async engine.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(db_module.dispose_engine())
    finally:
        loop.close()
    engine.dispose()


@pytest.fixture
def argosy_home_db(tmp_path, monkeypatch):
    """Set ARGOSY_HOME to tmp_path and initialize a file-backed SQLite at
    the standard db_file path. Use this in tests that exercise services
    that touch the DB but don't need a FastAPI app/client.

    The fixture also seeds a default user 'ariel' so catalog/audit-log
    inserts (which FK-CASCADE on users.id) don't fail.
    """
    import asyncio
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    settings = get_settings()
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)

    sync_url = f"sqlite:///{settings.db_file}"
    async_url = f"sqlite+aiosqlite:///{settings.db_file}"

    sync_engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(sync_engine)
    SessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False)

    db_module.init_engine(async_url)

    # Seed default 'ariel' user so the catalog/audit FKs don't fail.
    from argosy.state.models import User
    sess = SessionLocal()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
    finally:
        sess.close()

    yield tmp_path

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(db_module.dispose_engine())
    finally:
        loop.close()
    sync_engine.dispose()


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
