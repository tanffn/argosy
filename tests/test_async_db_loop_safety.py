"""Real SQLite concurrency across the API/thread-worker event-loop seam."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
from sqlalchemy.pool import NullPool, StaticPool

from argosy.state import db


def test_file_engine_survives_concurrent_distinct_event_loops(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    engine = db.init_engine(f"sqlite+aiosqlite:///{(tmp_path / 'loops.db').as_posix()}")
    async def setup():
        async with engine.begin() as connection:
            await connection.execute(text("create table observations (worker integer primary key)"))
    async def batch(worker):
        async def read():
            async with engine.connect() as connection:
                assert await connection.scalar(text("select 1")) == 1
                await asyncio.sleep(0.02)  # force the old 15-connection pool to wait
        await asyncio.gather(*(read() for _ in range(20)))
        async with db.get_session() as session:
            await session.execute(text("insert into observations values (:worker)"), {"worker": worker})
            await session.commit()
    async def verify():
        async with engine.connect() as connection:
            assert await connection.scalar(text("select count(*) from observations")) == 4
            assert await connection.scalar(text("pragma journal_mode")) == "wal"
            assert await connection.scalar(text("pragma busy_timeout")) == db.SQLITE_BUSY_TIMEOUT_MS
        await engine.dispose()
    asyncio.run(setup())
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(lambda n: asyncio.run(batch(n)), range(4)))
        asyncio.run(verify())
        assert isinstance(engine.pool, NullPool)
    finally:
        asyncio.run(engine.dispose())


def test_memory_database_keeps_its_connection():
    engine = db.create_shared_async_engine("sqlite+aiosqlite:///:memory:")
    assert isinstance(engine.pool, StaticPool)
    asyncio.run(engine.dispose())


def test_tenant_engines_use_same_loop_safe_factory(tmp_path, monkeypatch):
    from argosy.tenancy import database
    monkeypatch.setattr(database, "_ENGINES", {})
    monkeypatch.setattr(database, "_FACTORIES", {})
    monkeypatch.setattr(database, "_tenant_database_url", lambda user: f"sqlite+aiosqlite:///{(tmp_path / (user + '.db')).as_posix()}")
    try:
        a, b = database.get_tenant_engine("a"), database.get_tenant_engine("b")
        assert a is database.get_tenant_engine("a") and a is not b
        assert isinstance(a.pool, NullPool) and isinstance(b.pool, NullPool)
    finally:
        asyncio.run(database.reset_tenant_engines())
