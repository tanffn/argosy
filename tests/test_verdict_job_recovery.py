"""Real migrated DB contention: a queued verdict write cannot freeze its owner."""
import asyncio
import threading

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from argosy.orchestrator.loops.verdict_trigger_daily import VerdictTriggerDailyLoop
from argosy.services.verdict_registry import write_verdict
from argosy.state.models import ActionProposal, User


@pytest.mark.asyncio
async def test_waiting_verdict_writer_keeps_event_loop_alive_and_retries_without_duplicates(alembic_engine_at_head):
    engine = alembic_engine_at_head
    # A broken implementation fails quickly rather than blocking for 60s.
    @event.listens_for(engine, "connect")
    def timeout(connection, _record):
        connection.execute("PRAGMA busy_timeout=1500")
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id="ariel", plan="free"))
        write_verdict(session, user_id="ariel", subject="TEST", verdict="HOLD", conviction="HIGH",
                      revisit_triggers=[{"kind": "price_below", "price": 100}], source_decision_run_id=999)
        session.commit()
    quote_finished = asyncio.Event()
    main_loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    worker_threads = []
    def quotes(session, user_id, subjects):
        worker_threads.append(threading.get_ident())
        main_loop.call_soon_threadsafe(quote_finished.set)
        return {"TEST": 90}
    job = VerdictTriggerDailyLoop(session_factory=factory, quotes_fn=quotes)
    blocker = engine.connect()
    blocker.exec_driver_sql("BEGIN IMMEDIATE")
    task = asyncio.create_task(job.tick())
    try:
        await asyncio.wait_for(quote_finished.wait(), timeout=3)
        await asyncio.sleep(0.05)
        assert not task.done(), "write should still be waiting while event loop remains responsive"
        blocker.rollback()  # Requires the event loop to remain alive!
        first = await asyncio.wait_for(task, timeout=3)
        assert "error" not in first and first["fired"] == 1
        second = await job.tick()
        assert second["unlock_proposal_ids"] == first["unlock_proposal_ids"]
        assert all(t != main_thread for t in worker_threads)
        with factory() as session:
            rows = list(session.scalars(select(ActionProposal)))
            assert len(rows) == 1 and rows[0].status == "open" and rows[0].kind == "note_only"
    finally:
        blocker.rollback()
        blocker.close()
        if not task.done():
            await task


@pytest.mark.asyncio
async def test_cancellation_does_not_release_job_while_worker_can_write(alembic_engine_at_head):
    factory = sessionmaker(alembic_engine_at_head, expire_on_commit=False)
    started, release = threading.Event(), threading.Event()
    def quotes(session, user_id, subjects):
        started.set()
        release.wait(timeout=3)
        return {}
    job = VerdictTriggerDailyLoop(session_factory=factory, quotes_fn=quotes)
    task = asyncio.create_task(job.tick())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done()
        task.cancel()  # backend grace-timeout cancellation after initial stop
        await asyncio.sleep(0.03)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
