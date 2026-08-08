"""Process-global SEC fair-access request-start pacing.

Every SEC path — async adapters and the sync enrich path — must reserve a
slot here. A second private ``time.sleep`` in the sync path used to bypass
this limiter and could stampede EDGAR under concurrent gathers.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Awaitable, Callable

MIN_SEC_REQUEST_INTERVAL_SECONDS = 0.11

_REQUEST_SLOT_LOCK = threading.Lock()
# One shared timeline for the default monotonic clock so sync + async share
# the same fair-access budget. Injected clocks keep their own key (tests).
_NEXT_REQUEST_START_BY_CLOCK: dict[Callable[[], float], float] = {}
_DEFAULT_CLOCK = time.monotonic


def validate_sec_request_interval(interval_seconds: float) -> None:
    if interval_seconds < MIN_SEC_REQUEST_INTERVAL_SECONDS:
        raise ValueError(
            "request_interval_seconds must be at least "
            f"{MIN_SEC_REQUEST_INTERVAL_SECONDS}"
        )


def _reserve_slot(
    *,
    clock: Callable[[], float],
    interval_seconds: float,
) -> float:
    """Return delay seconds until the reserved start (0 if immediate)."""
    now = clock()
    with _REQUEST_SLOT_LOCK:
        reserved_start = max(
            now,
            _NEXT_REQUEST_START_BY_CLOCK.get(clock, now),
        )
        _NEXT_REQUEST_START_BY_CLOCK[clock] = reserved_start + interval_seconds
    return max(0.0, reserved_start - now)


async def wait_for_sec_request_slot(
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    interval_seconds: float,
) -> None:
    """Reserve one process-global request-start slot, then wait asynchronously."""
    delay = _reserve_slot(clock=clock, interval_seconds=interval_seconds)
    if delay > 0:
        await sleep(delay)


def wait_for_sec_request_slot_sync(
    *,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    interval_seconds: float = MIN_SEC_REQUEST_INTERVAL_SECONDS,
) -> None:
    """Sync twin of ``wait_for_sec_request_slot`` — same process-global budget."""
    clk = clock or _DEFAULT_CLOCK
    delay = _reserve_slot(clock=clk, interval_seconds=interval_seconds)
    if delay > 0:
        sleep(delay)


__all__ = [
    "MIN_SEC_REQUEST_INTERVAL_SECONDS",
    "validate_sec_request_interval",
    "wait_for_sec_request_slot",
    "wait_for_sec_request_slot_sync",
]
