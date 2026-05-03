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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

_subscribers: list[asyncio.Queue[str]] = []
_lock = asyncio.Lock()


async def publish_event(name: str, payload: dict[str, Any]) -> None:
    """Broadcast an event to all subscribers. Dropping is OK if a queue is full."""
    msg = json.dumps({"event": name, "payload": payload})
    async with _lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:  # pragma: no cover - defensive
            continue


@asynccontextmanager
async def subscribe() -> AsyncIterator[asyncio.Queue[str]]:
    """Return a queue that receives JSON-serialized event strings.

    Use as `async with subscribe() as q: msg = await q.get()`.
    """
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
    async with _lock:
        _subscribers.append(q)
    try:
        yield q
    finally:
        async with _lock:
            try:
                _subscribers.remove(q)
            except ValueError:  # pragma: no cover - defensive
                pass


def _reset_for_tests() -> None:
    """Test helper: drop all subscribers."""
    _subscribers.clear()


__all__ = ["publish_event", "subscribe"]
