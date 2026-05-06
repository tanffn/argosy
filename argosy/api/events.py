"""WebSocket event pub/sub (Phase 2).

Dead-simple in-process broadcaster. The FastAPI WebSocket handler at
`/ws` subscribes; cadence loops and CLI commands publish events via
`publish_event(name, payload)`. No external broker — everything is
single-process for now.

Events emitted in Phase 2:
  - `daily_brief.ready` — payload: {brief_id, user_id, run_at}
  - `agent.run.finished` — payload: {agent_role, user_id}
  - `cadence.tick.fired` — payload: {loop, status}
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

_subscribers: list[asyncio.Queue[str]] = []
# threading.Lock instead of asyncio.Lock so that publish_event_threadsafe can
# be called from any thread (including asyncio.to_thread workers) without
# binding to a specific event loop.  The lock only guards short list mutations
# (_subscribers.append / .remove / list()), never an await, so a plain
# threading.Lock is sufficient and correct.
_lock = threading.Lock()

# The main event loop, captured the first time an async caller publishes or
# subscribes.  Worker threads (asyncio.to_thread) have no running loop of
# their own; they must schedule onto *this* loop via call_soon_threadsafe so
# that q.put_nowait wakes the correct event loop's futures.
_main_loop: asyncio.AbstractEventLoop | None = None


def _capture_main_loop() -> None:
    """Record the running loop as the main loop if not already captured."""
    global _main_loop
    if _main_loop is None:
        try:
            _main_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass


async def publish_event(name: str, payload: dict[str, Any]) -> None:
    """Broadcast an event to all subscribers. Dropping is OK if a queue is full."""
    _capture_main_loop()
    msg = json.dumps({"event": name, "payload": payload})
    with _lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:  # pragma: no cover - defensive
            continue


def publish_event_threadsafe(name: str, payload: dict[str, Any]) -> None:
    """Sync→async bridge: publish an event from any thread.

    Call this from synchronous code (route handlers, orchestrator flows,
    monthly_cycle worker threads) instead of await-ing publish_event directly.

    Strategy:
    - If the calling thread has a running loop (e.g. called from async code),
      schedule via loop.call_soon_threadsafe so we don't block.
    - Otherwise check if we captured a main loop (the normal case: monthly_cycle
      runs inside asyncio.to_thread — the main loop is running but not visible
      in the worker thread).  Schedule there so put_nowait wakes the right loop.
    - Last resort: run a one-shot loop.  Because _lock is a threading.Lock this
      is safe; there is no cross-loop asyncio.Lock binding to worry about.
      NOTE: in this path the event is delivered to subscribers on a fresh loop,
      which means subscribers created on a different loop will not see it.  This
      path is only hit in standalone scripts / tests with no live main loop.

    Any failure is swallowed; event publishing must never break primary work.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    target_loop = loop if loop is not None and loop.is_running() else _main_loop

    try:
        if target_loop is not None and target_loop.is_running():
            # Schedule onto the target loop without blocking the calling thread.
            target_loop.call_soon_threadsafe(
                target_loop.create_task,
                publish_event(name, payload),
            )
        else:
            # No live loop available — one-shot execution.
            asyncio.run(publish_event(name, payload))
    except Exception:  # pragma: no cover - defensive
        pass


@asynccontextmanager
async def subscribe() -> AsyncIterator[asyncio.Queue[str]]:
    """Return a queue that receives JSON-serialized event strings.

    Use as `async with subscribe() as q: msg = await q.get()`.
    """
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
    with _lock:
        _subscribers.append(q)
    try:
        yield q
    finally:
        with _lock:
            try:
                _subscribers.remove(q)
            except ValueError:  # pragma: no cover - defensive
                pass


def _reset_for_tests() -> None:
    """Test helper: drop all subscribers and clear the captured main loop."""
    global _main_loop
    _subscribers.clear()
    _main_loop = None


__all__ = ["publish_event", "publish_event_threadsafe", "subscribe"]
