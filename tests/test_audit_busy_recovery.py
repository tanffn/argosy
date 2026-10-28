import asyncio
import sqlite3

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from argosy.execution.audit import record_audit_event
from argosy.state import db as db_mod
from argosy.state.models import AuditLog, User


@pytest.mark.asyncio
async def test_owned_audit_write_retries_real_sqlite_busy_once_without_duplicates(tmp_path, monkeypatch):
    path = tmp_path / "audit.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 0.01})
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(AuditLog.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "get_session", factory)
    async with factory() as session:
        session.add(User(id="test"))
        await session.commit()
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    async def release():
        await asyncio.sleep(0.08)
        blocker.rollback()
    release_task = asyncio.create_task(release())
    try:
        row_id = await record_audit_event(user_id="test", event_type="backup.completed")
        await release_task
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(AuditLog)) == 1
            assert (await session.get(AuditLog, row_id)).event_type == "backup.completed"
    finally:
        await release_task
        blocker.close()
        await engine.dispose()
