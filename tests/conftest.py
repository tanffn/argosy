"""Pytest fixtures: in-memory SQLite + FastAPI test client."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from argosy.state import db as db_module
from argosy.state.models import Base

# ---------------------------------------------------------------------------
# Logging / structlog isolation
#
# argosy.logging.configure_logging() wires structlog to the stdlib logging
# machinery and sets root.handlers = [file_handler, stderr_handler].  It is
# guarded by _CONFIGURED so it only runs once per process.
#
# Problem: when configure_logging() is first called at *collection time*
# (triggered by module-level `log = get_logger(...)` calls in imported
# modules), root.handlers is replaced BEFORE pytest's per-test caplog
# fixture can add its LogCaptureHandler.  Then, inside a test, pytest adds
# its handler on top of the existing list — that works correctly for stdlib
# loggers.
#
# However, with structlog's `cache_logger_on_first_use=True` (now disabled
# in argosy/logging.py), BoundLoggerLazyProxy objects cache their processor
# chain + underlying stdlib logger on first use.  If first use happens in a
# test WITHOUT a caplog context, the caplog handler is NOT in the handler
# chain for subsequent tests.
#
# The belt-and-suspenders fix here:
#   1. Call configure_logging() at module-import time so it runs before any
#      test's caplog fixture tries to set up.  This ensures root.handlers is
#      always [file_handler, stderr_handler] before caplog adds its own handler.
#   2. Add a per-test autouse fixture that resets global logging state that
#      pytest's caplog may leave dirty after exceptions in prior tests
#      (specifically, manager.disable can be left non-zero, causing warning
#      records to be silently discarded).
# ---------------------------------------------------------------------------
from argosy.logging import configure_logging as _argosy_configure_logging
_argosy_configure_logging()


@pytest.fixture(autouse=True)
def _guard_alternatives_phase(request, monkeypatch):
    """Never let ``run_synthesis`` fire the REAL alternatives phase in tests.

    ``run_synthesis`` calls ``run_alternatives_phase`` unconditionally, which
    spawns real sourcer/reviewer/FM agents (live claude.exe). With no pytest
    guard, any synthesis-flow test that doesn't stub it HANGS the suite (the
    documented gotcha). Default it to a deterministic 0% sleeve for every test;
    ``test_alternatives_phase`` (which exercises the real function by stubbing
    its internals) opts out. Tests that stub it themselves win — their
    monkeypatch runs after this fixture.
    """
    mod = getattr(request.node, "module", None)
    mod_name = getattr(mod, "__name__", "") or ""
    if not mod_name.endswith("test_alternatives_phase"):
        try:
            from argosy.orchestrator.flows.plan_synthesis import alternatives_phase as _ap

            def _stub(*, user_id, macro_context):
                return _ap._zero(
                    "0_percent",
                    "test guard: alternatives phase stubbed (no live LLM call)",
                    [],
                )

            monkeypatch.setattr(_ap, "run_alternatives_phase", _stub)
        except Exception:  # noqa: BLE001 — module shape changed; don't block tests
            pass
    yield


@pytest.fixture(autouse=True)
def _structlog_isolation():
    """Ensure clean structlog / stdlib-logging state for every test.

    With ``cache_logger_on_first_use=True`` (disabled in argosy/logging.py,
    but kept here as belt-and-suspenders), a structlog BoundLoggerLazyProxy
    that was first-used in a test WITHOUT a caplog handler active will cache
    a processor chain that skips caplog for all subsequent tests.

    This fixture clears structlog's contextvars (which are test-scoped
    thread-locals and should not bleed across tests) and also resets the
    manager-level ``logging.disable()`` threshold that pytest's caplog may
    leave non-zero after an unexpected exception in a prior test's
    ``at_level`` context.
    """
    import structlog
    structlog.contextvars.clear_contextvars()
    # Reset any stale manager-level disable level so WARNING records are
    # never silently dropped by a prior test's uncleaned caplog state.
    original_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    yield
    # Restore the disable level (in case a test intentionally set it).
    logging.disable(original_disable)


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


@pytest.fixture()
def expense_client(client_with_db, tmp_path, monkeypatch):
    """client_with_db augmented with:
      - ARGOSY_HOME → tmp_path (so catalog_upload writes to a throw-away dir)
      - a seeded User row so FK-aware sessions don't fail
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    # Seed the 'ariel' user into the test DB so UserFile FK is satisfied even
    # when SQLite FK enforcement is on.
    from argosy.state.models import User
    SessionLocal = client_with_db.app.state.session_factory
    with SessionLocal() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
            s.commit()

    yield client_with_db

    reload_settings()


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


@pytest.fixture
def db_session_with_zero_prior(tmp_path):
    """Two months of data for compute_hero_stats_monthly's zero-prior branch.

    March 2026: a single credit (income) row only — zero spending.
    April 2026: ten ₪500 debits — ₪5000 spending.

    For ``month='2026-04'`` the prior month (March) has zero spending, so
    ``mom_delta_pct`` must be ``None`` (not infinity).
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

    db_path = tmp_path / "zero_prior.db"
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
            user_id="test", sha256="e" * 64,
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

        # March 2026: zero spending; one credit/income row exists so the
        # month is "present" but spending is 0.
        march = date(2026, 3, 1)
        stmt_mar = ExpenseStatement(
            user_id="test", source_id=src.id, file_id=f.id,
            period_start=march, period_end=date(2026, 3, 28),
            parsed_total_nis=Decimal("0"),
            declared_total_nis=Decimal("0"),
            parser_name="isracard", parser_version="0.1.0",
            status="parsed",
        )
        s.add(stmt_mar)
        s.flush()
        s.add(ExpenseTransaction(
            user_id="test", source_id=src.id, statement_id=stmt_mar.id,
            occurred_on=date(2026, 3, 1),
            merchant_raw="EMPLOYER", merchant_normalized="employer",
            amount_nis=Decimal("10000"),
            direction="credit", tx_type="regular",
            category_id=income_cat.id, category_source="user",
            category_confidence=Decimal("1.0"),
            raw_row_json="{}",
        ))

        # April 2026: ₪5000 spending across 10 debits.
        april = date(2026, 4, 1)
        stmt_apr = ExpenseStatement(
            user_id="test", source_id=src.id, file_id=f.id,
            period_start=april, period_end=date(2026, 4, 28),
            parsed_total_nis=Decimal("5000"),
            declared_total_nis=Decimal("5000"),
            parser_name="isracard", parser_version="0.1.0",
            status="parsed",
        )
        s.add(stmt_apr)
        s.flush()
        for i in range(10):
            day = min(i + 1, 28)
            s.add(ExpenseTransaction(
                user_id="test", source_id=src.id, statement_id=stmt_apr.id,
                occurred_on=date(2026, 4, day),
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
# /dashboard-monthly endpoint fixtures.
#
# These fixtures seed multiple months of expense data through the
# ``client_with_db`` TestClient so the /api/expenses/dashboard-monthly route
# can be hit end-to-end. They yield ``(TestClient, user_id, focal_month)``
# tuples so each test can call the endpoint with a focal month it knows has
# data.
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_seeded_data(client_with_db):
    """Seed 14 months of expenses ending 2026-04 for user 'tu1' and yield
    ``(TestClient, user_id, latest_month)``.

    Each month gets: 10 debit dining-out rows (₪500 each) + a salary credit
    (₪10000) on alternating months. This is enough for the chart_window to
    fill (12 of 14 months in view), the hero_stats trailing-12 to engage,
    and at least one category in top_categories.
    """
    from datetime import date
    from decimal import Decimal

    from argosy.state.models import (
        User, UserFile, ExpenseSource, ExpenseStatement,
        ExpenseTransaction, ExpenseCategory,
    )
    from argosy.services.expense_ingest.taxonomy_seed import (
        seed_system_defaults, seed_user_categories,
    )

    SF = client_with_db.app.state.session_factory
    user_id = "tu1"
    latest_month = "2026-04"

    with SF() as s:
        s.add(User(id=user_id, plan="free"))
        s.flush()
        seed_system_defaults(s)
        s.flush()
        seed_user_categories(s, user_id)
        s.flush()

        f = UserFile(
            user_id=user_id, sha256="m" * 64,
            original_name="seed", sanitized_name="seed",
            mime_type="application/octet-stream", kind="other",
            size_bytes=1, storage_path="/tmp/seed-monthly",
            source="chat_attachment",
        )
        s.add(f)
        s.flush()

        src = ExpenseSource(
            user_id=user_id, kind="card", issuer="isracard",
            external_id="7777", display_name="monthly-seed-card",
        )
        s.add(src)
        s.flush()

        spend_cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="dining_out.restaurants",
        ).one()
        income_cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="income.salary",
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
            period_end = date(month_start.year, month_start.month, 28)
            stmt = ExpenseStatement(
                user_id=user_id, source_id=src.id, file_id=f.id,
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
                    user_id=user_id, source_id=src.id, statement_id=stmt.id,
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
                    user_id=user_id, source_id=src.id, statement_id=stmt.id,
                    occurred_on=date(month_start.year, month_start.month, 1),
                    merchant_raw="EMPLOYER", merchant_normalized="employer",
                    amount_nis=Decimal("10000"),
                    direction="credit", tx_type="regular",
                    category_id=income_cat.id, category_source="user",
                    category_confidence=Decimal("1.0"),
                    raw_row_json="{}",
                ))
        s.commit()

    yield client_with_db, user_id, latest_month


@pytest.fixture
def client_with_short_history(client_with_db):
    """Seed only 2 months of expenses for user 'tu_short' so the 12-bar
    chart_window has to use padding bars.

    Yields ``(TestClient, user_id, latest_month)``.
    """
    from datetime import date
    from decimal import Decimal

    from argosy.state.models import (
        User, UserFile, ExpenseSource, ExpenseStatement,
        ExpenseTransaction, ExpenseCategory,
    )
    from argosy.services.expense_ingest.taxonomy_seed import (
        seed_system_defaults, seed_user_categories,
    )

    SF = client_with_db.app.state.session_factory
    user_id = "tu_short"
    latest_month = "2026-04"

    with SF() as s:
        s.add(User(id=user_id, plan="free"))
        s.flush()
        seed_system_defaults(s)
        s.flush()
        seed_user_categories(s, user_id)
        s.flush()

        f = UserFile(
            user_id=user_id, sha256="s" * 64,
            original_name="seed", sanitized_name="seed",
            mime_type="application/octet-stream", kind="other",
            size_bytes=1, storage_path="/tmp/seed-short",
            source="chat_attachment",
        )
        s.add(f)
        s.flush()

        src = ExpenseSource(
            user_id=user_id, kind="card", issuer="isracard",
            external_id="6666", display_name="short-seed-card",
        )
        s.add(src)
        s.flush()

        spend_cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="dining_out.restaurants",
        ).one()

        # 2 months only: 2026-03 and 2026-04.
        for month_start in (date(2026, 3, 1), date(2026, 4, 1)):
            period_end = date(month_start.year, month_start.month, 28)
            stmt = ExpenseStatement(
                user_id=user_id, source_id=src.id, file_id=f.id,
                period_start=month_start, period_end=period_end,
                parsed_total_nis=Decimal("1500"),
                declared_total_nis=Decimal("1500"),
                parser_name="isracard", parser_version="0.1.0",
                status="parsed",
            )
            s.add(stmt)
            s.flush()
            for i in range(3):
                day = min(i + 1, 28)
                s.add(ExpenseTransaction(
                    user_id=user_id, source_id=src.id, statement_id=stmt.id,
                    occurred_on=date(month_start.year, month_start.month, day),
                    merchant_raw=f"M{i}", merchant_normalized=f"m{i}",
                    amount_nis=Decimal("500"),
                    direction="debit", tx_type="regular",
                    category_id=spend_cat.id, category_source="user",
                    category_confidence=Decimal("1.0"),
                    raw_row_json="{}",
                ))
        s.commit()

    yield client_with_db, user_id, latest_month


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


# ---------------------------------------------------------------------------
# Wave A live-test backend-availability helper (shared across integration tests).
#
# The Wave A telemetry surface (citations_json + cache_input_tokens +
# cache_creation_tokens + thinking_tokens) is only populated on the api_key
# backend. The claude_code backend (Claude Agent SDK) intentionally does NOT
# surface those fields on its ResultMessage.usage dict, so any live test that
# asserts on them must SKIP cleanly when the configured backend is claude_code
# — not fail with confusing "thinking_tokens is 0" / "citations_json is None"
# style assertions.
#
# Centralized here so all three Wave A integration tests
# (analyst / researcher / decision) plus the cost-regression smoke share one
# definition. Previously each test rolled its own copy and decision drifted
# missing it entirely, which produced live-fails on claude_code instead of
# clean skips (Wave A finalization Issue 2).
# ---------------------------------------------------------------------------


def _api_key_backend_available() -> bool:
    """True iff Argosy is configured for the ``api_key`` Anthropic backend
    AND a key is reachable (env var or OS keychain).

    Used as a pytest ``skipif`` predicate by Wave A live integration tests
    that assert on api_key-only telemetry (citations + cache + thinking
    tokens). Returns False when:

      * settings load fails (no ARGOSY_HOME, no settings.toml, etc.)
      * backend is ``claude_code`` (or any non ``api_key`` value)
      * no ``ANTHROPIC_API_KEY`` env var AND no keychain entry under the
        configured key name

    Side-effect free; safe to call at module-import time / collection.
    """
    import os

    try:
        from argosy.config import get_settings

        if get_settings().anthropic.backend != "api_key":
            return False
    except Exception:
        return False
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    try:
        from argosy.config import get_settings
        from argosy.secrets import get_secret

        return bool(get_secret(get_settings().anthropic.keychain_key_name))
    except Exception:
        return False
