"""Exercise actual funnel bootstrap under a real competing SQLite writer."""
import asyncio
from datetime import UTC, datetime
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from argosy.services.decision_funnel.orchestrator import run_funnel
from argosy.state.models import User


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_funnel_bootstrap_waits_off_event_loop_and_drains_on_cancel(alembic_engine_at_head, cancel):
    engine = alembic_engine_at_head
    @event.listens_for(engine, "connect")
    def timeout(connection, _record):
        connection.execute("PRAGMA busy_timeout=1500")
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id="ariel", plan="free"))
        session.commit()
    write_started = asyncio.Event()
    loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    write_threads = []
    @event.listens_for(engine, "before_cursor_execute")
    def before_write(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO funnel_runs"):
            write_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(write_started.set)
    blocker = engine.connect()
    blocker.exec_driver_sql("BEGIN IMMEDIATE")
    task = asyncio.create_task(run_funnel(
        "ariel", now=datetime.now(UTC), session_factory=factory,
        settings=SimpleNamespace(decision_funnel_shadow=True, decision_funnel_stage3=False),
    ))
    try:
        await asyncio.wait_for(write_started.wait(), timeout=3)
        await asyncio.sleep(0.03)
        assert not task.done()
        if cancel:
            task.cancel()
            await asyncio.sleep(0.03)
            task.cancel()
            await asyncio.sleep(0.03)
            assert not task.done(), "cancellation must not detach a still-writing worker"
        blocker.rollback()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
        else:
            result = await asyncio.wait_for(task, timeout=3)
            assert result["status"] == "ok" and result["error_count"] == 0
        assert all(t != main_thread for t in write_threads)
    finally:
        blocker.rollback()
        blocker.close()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
