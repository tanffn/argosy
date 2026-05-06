"""Tests for argosy.api.events — pub/sub and thread-safe bridge.

Regression guard for I3: publish_event_threadsafe must work from a worker
thread that has no running asyncio event loop (the monthly_cycle path via
asyncio.to_thread).
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from argosy.api.events import _reset_for_tests, publish_event_threadsafe, subscribe


@pytest.fixture(autouse=True)
def _clean_subscribers():
    """Ensure subscriber list and captured main loop are cleared before/after each test."""
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_publish_event_threadsafe_from_worker_thread_no_loop():
    """I3 regression: publish_event_threadsafe called from a worker thread that
    has no running event loop must deliver the event to an active subscriber.

    This simulates the monthly_cycle.tick() path:
      - The main asyncio loop is running (asyncio.run / uvicorn).
      - run_synthesis is dispatched via asyncio.to_thread — the worker has no
        running loop of its own.
      - Under the old asyncio.Lock, the worker's asyncio.run() created a new
        loop that the Lock was not bound to, causing a RuntimeError swallowed
        by the bare ``except Exception: pass``.
      - With threading.Lock + _main_loop capture, the worker schedules onto
        the captured main loop and the subscriber receives the event correctly.
    """
    received: list[dict[str, Any]] = []

    async def _runner():
        # First await inside the main loop captures _main_loop.
        from argosy.api import events as ev_mod
        import argosy.api.events as _ev

        # Ensure capture happens by calling publish_event once (no subscribers yet).
        await _ev.publish_event("_warmup", {})

        async with subscribe() as q:
            # Signal the worker it can publish now (subscriber is registered).
            ready.set()

            # Simulate asyncio.to_thread: spawn a plain thread that calls the sync bridge.
            # The thread has no running loop — it must use _main_loop.
            def _worker():
                # Verify we truly have no running loop in this thread.
                try:
                    asyncio.get_running_loop()
                    has_loop = True
                except RuntimeError:
                    has_loop = False
                assert not has_loop, "worker thread should have no running loop"
                publish_event_threadsafe("test.event", {"value": 42})

            t = threading.Thread(target=_worker)
            t.start()
            t.join(timeout=5.0)

            # Give the scheduled task a chance to run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            msg_raw = await asyncio.wait_for(q.get(), timeout=3.0)
            received.append(json.loads(msg_raw))

    ready = threading.Event()
    asyncio.run(_runner())

    assert len(received) == 1, f"expected 1 event, got {received!r}"
    assert received[0]["event"] == "test.event"
    assert received[0]["payload"]["value"] == 42


def test_publish_event_threadsafe_from_async_context():
    """publish_event_threadsafe called from an async context (running loop)
    should schedule onto the current loop without blocking.
    """
    received: list[Any] = []

    async def _runner():
        async with subscribe() as q:
            # Call threadsafe bridge from inside the running loop.
            publish_event_threadsafe("async.event", {"x": 1})
            # Give the loop a couple of ticks to process the scheduled task.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            msg_raw = await asyncio.wait_for(q.get(), timeout=2.0)
            received.append(json.loads(msg_raw))

    asyncio.run(_runner())

    assert len(received) == 1
    assert received[0]["event"] == "async.event"
