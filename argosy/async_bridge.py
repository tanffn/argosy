"""Safe bridge for synchronous services that consume async adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor


def run_async_from_sync[T](factory: Callable[[], Awaitable[T]]) -> T:
    """Run an awaitable factory from sync code, with or without an active loop.

    The factory shape is intentional: a coroutine is created only in the loop
    that will await it, so an early bridge failure cannot leak an un-awaited
    coroutine.  When the caller already owns an event loop, a dedicated worker
    thread owns the temporary loop; otherwise ``asyncio.run`` is sufficient.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    def _worker() -> T:
        return asyncio.run(factory())

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="argosy-async-bridge") as pool:
        return pool.submit(_worker).result()


__all__ = ["run_async_from_sync"]
