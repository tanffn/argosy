"""Process-global SEC fair-access request-start pacing."""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable

MIN_SEC_REQUEST_INTERVAL_SECONDS = 0.11

_REQUEST_SLOT_LOCK = threading.Lock()
_NEXT_REQUEST_START_BY_CLOCK: dict[Callable[[], float], float] = {}


def validate_sec_request_interval(interval_seconds: float) -> None:
    if interval_seconds < MIN_SEC_REQUEST_INTERVAL_SECONDS:
        raise ValueError(
            "request_interval_seconds must be at least "
            f"{MIN_SEC_REQUEST_INTERVAL_SECONDS}"
        )


async def wait_for_sec_request_slot(
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    interval_seconds: float,
) -> None:
    """Reserve one process-global request-start slot, then wait asynchronously."""
    now = clock()
    with _REQUEST_SLOT_LOCK:
        reserved_start = max(
            now,
            _NEXT_REQUEST_START_BY_CLOCK.get(clock, now),
        )
        _NEXT_REQUEST_START_BY_CLOCK[clock] = (
            reserved_start + interval_seconds
        )
    delay = reserved_start - now
    if delay > 0:
        await sleep(delay)


__all__ = [
    "MIN_SEC_REQUEST_INTERVAL_SECONDS",
    "validate_sec_request_interval",
    "wait_for_sec_request_slot",
]
