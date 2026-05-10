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


@pytest.fixture
def db_session_with_seeded_user(tmp_path):
    """In-memory-style file-backed SQLite session seeded with 14 months of
    expense transactions for user_id='test'.

    Pattern:
      - 14 months ending at date(2026, 4, 1), working backwards.
      - Every month: ~10 debit ₪500 dining_out.restaurants rows (₪5000 spend).
      - Alternating months: a single ₪10000 income.salary credit. Months
        without that credit produce income == 0 — the zero-income branch.
    """
    from datetime import date
    from decimal import Decimal

    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import (
        Base, User, UserFile, ExpenseSource, ExpenseStatement,
        ExpenseTransaction, ExpenseCategory,
    )
    from argosy.services.expense_ingest.taxonomy_seed import (
        seed_system_defaults, seed_user_categories,
    )

    db_path = tmp_path / "savings_rate_trend.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="test", plan="free"))
        s.flush()
        seed_system_defaults(s)
        s.flush()
        seed_user_categories(s, "test")
        s.flush()

        f = UserFile(
            user_id="test", sha256="b" * 64,
            original_name="seed", sanitized_name="seed",
            mime_type="application/octet-stream", kind="other",
            size_bytes=1, storage_path="/tmp/seed", source="chat_attachment",
        )
        s.add(f)
        s.flush()

        src = ExpenseSource(
            user_id="test", kind="card", issuer="isracard",
            external_id="9999", display_name="seed-card",
        )
        s.add(src)
        s.flush()

        spend_cat = s.query(ExpenseCategory).filter_by(
            user_id="test", slug="dining_out.restaurants",
        ).one()
        income_cat = s.query(ExpenseCategory).filter_by(
            user_id="test", slug="income.salary",
        ).one()

        # 14 months ending 2026-04, oldest first.
        anchor = date(2026, 4, 1)
        months: list[date] = []
        y, m = anchor.year, anchor.month
        for _ in range(14):
            months.append(date(y, m, 1))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        months.reverse()

        for idx, month_start in enumerate(months):
            # End-of-month bound (use day 28 to stay safe on Feb).
            period_end = date(month_start.year, month_start.month, 28)
            stmt = ExpenseStatement(
                user_id="test", source_id=src.id, file_id=f.id,
                period_start=month_start, period_end=period_end,
                parsed_total_nis=Decimal("5000"),
                declared_total_nis=Decimal("5000"),
                parser_name="isracard", parser_version="0.1.0",
                status="parsed",
            )
            s.add(stmt)
            s.flush()

            # 10 debit dining_out.restaurants rows of ₪500 each.
            for i in range(10):
                day = min(i + 1, 28)
                s.add(ExpenseTransaction(
                    user_id="test", source_id=src.id, statement_id=stmt.id,
                    occurred_on=date(month_start.year, month_start.month, day),
                    merchant_raw=f"M{i}", merchant_normalized=f"m{i}",
                    amount_nis=Decimal("500"),
                    direction="debit", tx_type="regular",
                    category_id=spend_cat.id, category_source="user",
                    category_confidence=Decimal("1.0"),
                    raw_row_json="{}",
                ))

            # Alternating months: salary credit. Even idx → income; odd →
            # zero income (exercises the zero-income branch).
            if idx % 2 == 0:
                s.add(ExpenseTransaction(
                    user_id="test", source_id=src.id, statement_id=stmt.id,
                    occurred_on=date(month_start.year, month_start.month, 1),
                    merchant_raw="EMPLOYER", merchant_normalized="employer",
                    amount_nis=Decimal("10000"),
                    direction="credit", tx_type="regular",
                    category_id=income_cat.id, category_source="user",
                    category_confidence=Decimal("1.0"),
                    raw_row_json="{}",
                ))
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def db_session_short_history(tmp_path):
    """Like ``db_session_with_seeded_user`` but seeds only 4 months of data.

    Used to exercise the "insufficient_history" branch in helpers that
    compare a current vs a prior period (e.g. compute_top_movers).
    """
    from datetime import date
    from decimal import Decimal

    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import (
        Base, User, UserFile, ExpenseSource, ExpenseStatement,
        ExpenseTransaction, ExpenseCategory,
    )
    from argosy.services.expense_ingest.taxonomy_seed import (
        seed_system_defaults, seed_user_categories,
    )

    db_path = tmp_path / "short_history.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="test", plan="free"))
        s.flush()
        seed_system_defaults(s)
        s.flush()
        seed_user_categories(s, "test")
        s.flush()

        f = UserFile(
            user_id="test", sha256="c" * 64,
            original_name="seed", sanitized_name="seed",
            mime_type="application/octet-stream", kind="other",
            size_bytes=1, storage_path="/tmp/seed", source="chat_attachment",
        )
        s.add(f)
        s.flush()

        src = ExpenseSource(
            user_id="test", kind="card", issuer="isracard",
            external_id="9999", display_name="seed-card",
        )
        s.add(src)
        s.flush()

        spend_cat = s.query(ExpenseCategory).filter_by(
            user_id="test", slug="dining_out.restaurants",
        ).one()
        income_cat = s.query(ExpenseCategory).filter_by(
            user_id="test", slug="income.salary",
        ).one()

        # 4 months ending 2026-04, oldest first.
        anchor = date(2026, 4, 1)
        months: list[date] = []
        y, m = anchor.year, anchor.month
        for _ in range(4):
            months.append(date(y, m, 1))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        months.reverse()

        for idx, month_start in enumerate(months):
            period_end = date(month_start.year, month_start.month, 28)
            stmt = ExpenseStatement(
                user_id="test", source_id=src.id, file_id=f.id,
                period_start=month_start, period_end=period_end,
                parsed_total_nis=Decimal("5000"),
                declared_total_nis=Decimal("5000"),
                parser_name="isracard", parser_version="0.1.0",
                status="parsed",
            )
            s.add(stmt)
            s.flush()

            for i in range(10):
                day = min(i + 1, 28)
                s.add(ExpenseTransaction(
                    user_id="test", source_id=src.id, statement_id=stmt.id,
                    occurred_on=date(month_start.year, month_start.month, day),
                    merchant_raw=f"M{i}", merchant_normalized=f"m{i}",
                    amount_nis=Decimal("500"),
                    direction="debit", tx_type="regular",
                    category_id=spend_cat.id, category_source="user",
                    category_confidence=Decimal("1.0"),
                    raw_row_json="{}",
                ))

            if idx % 2 == 0:
                s.add(ExpenseTransaction(
                    user_id="test", source_id=src.id, statement_id=stmt.id,
                    occurred_on=date(month_start.year, month_start.month, 1),
                    merchant_raw="EMPLOYER", merchant_normalized="employer",
                    amount_nis=Decimal("10000"),
                    direction="credit", tx_type="regular",
                    category_id=income_cat.id, category_source="user",
                    category_confidence=Decimal("1.0"),
                    raw_row_json="{}",
                ))
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def db_session_long_history(tmp_path):
    """Like ``db_session_with_seeded_user`` but seeds 17 months of data.

    Oldest month: 2024-12. Newest month: 2026-04 (inclusive). Used to
    exercise the ChartWindow A-rule sliding-window logic with a deep
    enough range that the centring case has data on both sides.
    """
    from datetime import date
    from decimal import Decimal

    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import (
        Base, User, UserFile, ExpenseSource, ExpenseStatement,
        ExpenseTransaction, ExpenseCategory,
    )
    from argosy.services.expense_ingest.taxonomy_seed import (
        seed_system_defaults, seed_user_categories,
    )

    db_path = tmp_path / "long_history.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="test", plan="free"))
        s.flush()
        seed_system_defaults(s)
        s.flush()
        seed_user_categories(s, "test")
        s.flush()

        f = UserFile(
            user_id="test", sha256="d" * 64,
            original_name="seed", sanitized_name="seed",
            mime_type="application/octet-stream", kind="other",
            size_bytes=1, storage_path="/tmp/seed", source="chat_attachment",
        )
        s.add(f)
        s.flush()

        src = ExpenseSource(
            user_id="test", kind="card", issuer="isracard",
            external_id="9999", display_name="seed-card",
        )
        s.add(src)
        s.flush()

        spend_cat = s.query(ExpenseCategory).filter_by(
            user_id="test", slug="dining_out.restaurants",
        ).one()

        # 17 months ending 2026-04, oldest first => oldest is 2024-12.
        anchor = date(2026, 4, 1)
        months: list[date] = []
        y, m = anchor.year, anchor.month
        for _ in range(17):
            months.append(date(y, m, 1))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        months.reverse()

        for month_start in months:
            period_end = date(month_start.year, month_start.month, 28)
            stmt = ExpenseStatement(
                user_id="test", source_id=src.id, file_id=f.id,
                period_start=month_start, period_end=period_end,
                parsed_total_nis=Decimal("2500"),
                declared_total_nis=Decimal("2500"),
                parser_name="isracard", parser_version="0.1.0",
                status="parsed",
            )
            s.add(stmt)
            s.flush()

            # 5 debit dining_out.restaurants rows of ₪500 each (~₪2500/month).
            for i in range(5):
                day = min(i + 1, 28)
                s.add(ExpenseTransaction(
                    user_id="test", source_id=src.id, statement_id=stmt.id,
                    occurred_on=date(month_start.year, month_start.month, day),
                    merchant_raw=f"M{i}", merchant_normalized=f"m{i}",
                    amount_nis=Decimal("500"),
                    direction="debit", tx_type="regular",
                    category_id=spend_cat.id, category_source="user",
                    category_confidence=Decimal("1.0"),
                    raw_row_json="{}",
                ))
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


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
