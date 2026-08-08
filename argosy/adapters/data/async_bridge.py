"""Sync↔async bridge for adapter / cache / catalog work.

Problem
-------
The shared SQLAlchemy ``AsyncEngine`` (aiosqlite) binds connection
futures to the event loop that first checked them out. Sync callers
that bridge with ``asyncio.run(...)`` create a *new* loop each call.
When the process already has a live main loop (FastAPI + cadence jobs
via ``asyncio.to_thread``), that detonates as::

    Queue is bound to a different event loop

Seen 1,065 times in ``logs/app/application.log`` (2026-06-14 →
2026-08-07): snapshot quotes, FX, predictions evaluator, thesis
monitor feeds, payslip catalog.

Fix
---
``run_coro_sync`` marshals the coroutine onto a *long-lived* loop:

1. Prefer the captured FastAPI / app main loop when it is running
   (worker threads then share the same aiosqlite pool as the app).
2. Otherwise use a dedicated daemon-thread bridge loop (CLI / tests
   with no live main loop).

Never use ``asyncio.run`` for work that touches ``cached_call`` /
``get_session``.

Nested-async rule
-----------------
If the *calling* thread already has a running loop that **is** the
target loop, synchronous waiting is impossible (deadlock). We refuse
immediately with a loud ``RuntimeError`` telling the caller to
``await`` instead. Offloading the wait only helps when the caller's
loop is a *different* loop from the target.

Health / degradation signal
---------------------------
Any failure of this subsystem to return real data — event-loop
mismatch, bridge timeout, same-loop misuse — increments a
**recency-scoped** infra counter (default 15 min window). Lifetime
totals are retained separately. `/health` is ``degraded`` while the
window is non-empty; it recovers when the window empties.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections import deque
from collections.abc import Coroutine
from typing import Any, TypeVar

from argosy.logging import get_logger

_log = get_logger("argosy.adapters.async_bridge")

T = TypeVar("T")

# Captured app loop (FastAPI startup / first async publish). Optional —
# when unset, sync callers fall through to the dedicated bridge loop.
_main_loop: asyncio.AbstractEventLoop | None = None
_main_loop_lock = threading.Lock()

# Dedicated long-lived loop for sync contexts with no live main loop.
_bridge_loop: asyncio.AbstractEventLoop | None = None
_bridge_thread: threading.Thread | None = None
_bridge_ready = threading.Event()
_bridge_lock = threading.Lock()

# Infra data-path failures — lifetime total + recency window for health.
# Includes: event_loop_mismatch, bridge_timeout, same_loop_deadlock.
_mismatch_lifetime = 0
_mismatch_times: deque[float] = deque()  # monotonic timestamps
_mismatch_lock = threading.Lock()
_MISMATCH_HEALTH_WINDOW_S = 15 * 60.0

_DEFAULT_TIMEOUT_S = 120.0

_SAME_LOOP_MSG = (
    "run_coro_sync called from the target event loop's own thread; "
    "await the coroutine directly instead of bridging "
    "(blocking wait would deadlock the loop)."
)


def capture_main_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Record the app event loop that owns the primary async DB pool.

    Safe to call repeatedly; the first non-closed loop wins until
    ``reset_for_tests`` clears it.
    """
    global _main_loop
    target = loop
    if target is None:
        try:
            target = asyncio.get_running_loop()
        except RuntimeError:
            return
    with _main_loop_lock:
        if _main_loop is None or _main_loop.is_closed():
            _main_loop = target


def get_main_loop() -> asyncio.AbstractEventLoop | None:
    with _main_loop_lock:
        if _main_loop is not None and not _main_loop.is_closed():
            return _main_loop
        return None


def _ensure_bridge_loop() -> asyncio.AbstractEventLoop:
    """Start (once) the daemon-thread event loop used when no main loop."""
    global _bridge_loop, _bridge_thread
    with _bridge_lock:
        if _bridge_loop is not None and not _bridge_loop.is_closed():
            return _bridge_loop
        _bridge_ready.clear()

        def _runner() -> None:
            global _bridge_loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _bridge_loop = loop
            _bridge_ready.set()
            loop.run_forever()

        _bridge_thread = threading.Thread(
            target=_runner,
            name="argosy-async-bridge",
            daemon=True,
        )
        _bridge_thread.start()
    if not _bridge_ready.wait(timeout=10.0):
        raise RuntimeError("argosy async bridge loop failed to start")
    assert _bridge_loop is not None
    return _bridge_loop


def _resolve_target_loop() -> tuple[asyncio.AbstractEventLoop, str]:
    """Return ``(loop, which)`` where which is ``'main'`` or ``'bridge'``."""
    main = get_main_loop()
    if main is not None and main.is_running():
        return main, "main"
    # Also accept events.py's captured loop when our capture was missed.
    try:
        from argosy.api import events as events_mod

        ev_loop = getattr(events_mod, "_main_loop", None)
        if ev_loop is not None and ev_loop.is_running():
            capture_main_loop(ev_loop)
            return ev_loop, "main"
    except Exception:  # noqa: BLE001 — optional coupling
        pass
    return _ensure_bridge_loop(), "bridge"


def _close_coro(coro: Coroutine[Any, Any, Any]) -> None:
    """Drop an unused coroutine to avoid 'never awaited' warnings."""
    try:
        coro.close()
    except Exception:  # noqa: BLE001
        pass


def _submit_and_wait_on(
    target: asyncio.AbstractEventLoop,
    which: str,
    coro: Coroutine[Any, Any, T],
    *,
    timeout: float | None,
) -> T:
    fut = asyncio.run_coroutine_threadsafe(coro, target)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        fut.cancel()
        note_infra_data_failure(
            kind="bridge_timeout",
            scope=f"run_coro_sync.{which}",
            error=f"timed out after {timeout}s on {which} loop",
            timeout_s=timeout,
            target_loop=which,
        )
        raise TimeoutError(
            f"run_coro_sync timed out after {timeout}s on {which} loop"
        ) from exc


def run_coro_sync(
    coro: Coroutine[Any, Any, T],
    *,
    timeout: float | None = _DEFAULT_TIMEOUT_S,
) -> T:
    """Drive ``coro`` to completion from any sync (or nested-async) context.

    * No running loop in this thread → submit to main/bridge and block
      (with ``timeout``; default 120s — never hang forever).
    * Running loop **is** the target loop → refuse immediately
      (``RuntimeError``). Blocking would deadlock; ``await`` instead.
    * Running loop is a **different** loop from the target → offload the
      blocking wait to a worker thread (safe; no deadlock).
    * No captured main loop → dedicated bridge loop (CLI / scripts).
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    target, which = _resolve_target_loop()

    if running is not None and running is target:
        _close_coro(coro)
        note_infra_data_failure(
            kind="same_loop_deadlock",
            scope="run_coro_sync.same_loop",
            error=_SAME_LOOP_MSG,
            target_loop=which,
        )
        raise RuntimeError(_SAME_LOOP_MSG)

    if running is None:
        return _submit_and_wait_on(target, which, coro, timeout=timeout)

    # Caller has a *different* running loop from the target — offload the
    # blocking wait so we do not stall that other loop.
    wait_timeout = (timeout + 5.0) if timeout is not None else None

    def _in_worker() -> T:
        return _submit_and_wait_on(target, which, coro, timeout=timeout)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="argosy-coro-wait"
    ) as pool:
        try:
            return pool.submit(_in_worker).result(timeout=wait_timeout)
        except concurrent.futures.TimeoutError as exc:
            note_infra_data_failure(
                kind="bridge_timeout",
                scope="run_coro_sync.nested_offload",
                error=f"nested offload timed out after {wait_timeout}s",
                timeout_s=timeout,
                target_loop=which,
            )
            raise TimeoutError(
                f"run_coro_sync nested offload timed out after {wait_timeout}s"
            ) from exc


def is_event_loop_mismatch(exc: BaseException) -> bool:
    """True when ``exc`` is the aiosqlite / asyncio cross-loop failure."""
    msg = str(exc).lower()
    return (
        "bound to a different event loop" in msg
        or "attached to a different loop" in msg
        or "got future attached to a different loop" in msg
    )


def is_bridge_timeout(exc: BaseException) -> bool:
    """True when ``exc`` is a ``run_coro_sync`` timeout."""
    if isinstance(exc, TimeoutError):
        return True
    msg = str(exc).lower()
    return "run_coro_sync timed out" in msg or "nested offload timed out" in msg


def is_infra_data_failure(exc: BaseException) -> bool:
    """True for any bridge failure that must surface as infra degradation."""
    return (
        is_event_loop_mismatch(exc)
        or is_bridge_timeout(exc)
        or (
            isinstance(exc, RuntimeError)
            and "target event loop's own thread" in str(exc)
        )
    )


def _prune_mismatch_times(now: float, window_s: float) -> None:
    """Drop timestamps older than the health window. Caller holds the lock."""
    cutoff = now - window_s
    while _mismatch_times and _mismatch_times[0] < cutoff:
        _mismatch_times.popleft()


def note_infra_data_failure(
    *,
    kind: str,
    scope: str,
    error: str,
    **extra: Any,
) -> int:
    """Log + count + publish an infra data-path failure. Returns lifetime count.

    Every path where the bridge/adapters fail to return real data must call
    this (or ``note_event_loop_mismatch``) so ``/health`` and job summaries
    see the degradation. Kinds: ``event_loop_mismatch``, ``bridge_timeout``,
    ``same_loop_deadlock``.
    """
    global _mismatch_lifetime
    now = time.monotonic()
    with _mismatch_lock:
        _mismatch_lifetime += 1
        _mismatch_times.append(now)
        _prune_mismatch_times(now, _MISMATCH_HEALTH_WINDOW_S)
        lifetime = _mismatch_lifetime
        recent = len(_mismatch_times)
    payload = {
        "kind": kind,
        "scope": scope,
        "error": (error or "")[:500],
        "process_count": lifetime,
        "recent_count": recent,
        "health_window_s": _MISMATCH_HEALTH_WINDOW_S,
        **extra,
    }
    _log.error("infra.data_path_failure", **payload)
    try:
        from argosy.api.events import publish_event_threadsafe

        publish_event_threadsafe(
            "infra.data_path_failure",
            {k: v for k, v in payload.items() if k != "error"}
            | {"error": (error or "")[:300]},
        )
    except Exception:  # noqa: BLE001 — publishing must never break primary work
        pass
    return lifetime


def note_event_loop_mismatch(
    *,
    scope: str,
    error: str,
    **extra: Any,
) -> int:
    """Log + count a cross-loop aiosqlite failure. Returns lifetime count."""
    return note_infra_data_failure(
        kind="event_loop_mismatch",
        scope=scope,
        error=error,
        **extra,
    )


def mismatch_count() -> int:
    """Lifetime infra data-path failure count since boot (or last reset)."""
    with _mismatch_lock:
        return _mismatch_lifetime


def recent_mismatch_count(
    *,
    window_s: float | None = None,
    now: float | None = None,
) -> int:
    """Infra failures inside the health window (default 15 minutes)."""
    w = _MISMATCH_HEALTH_WINDOW_S if window_s is None else window_s
    t = time.monotonic() if now is None else now
    with _mismatch_lock:
        _prune_mismatch_times(t, w)
        return len(_mismatch_times)


def mismatch_health_window_s() -> float:
    return _MISMATCH_HEALTH_WINDOW_S


def set_mismatch_health_window_for_tests(window_s: float) -> None:
    """Tests only: shrink/expand the recency window (and prune)."""
    global _MISMATCH_HEALTH_WINDOW_S
    with _mismatch_lock:
        _MISMATCH_HEALTH_WINDOW_S = float(window_s)
        _prune_mismatch_times(time.monotonic(), _MISMATCH_HEALTH_WINDOW_S)


def reset_for_tests() -> None:
    """Drop captured main loop + infra counters. Bridge loop stays alive."""
    global _main_loop, _mismatch_lifetime, _MISMATCH_HEALTH_WINDOW_S
    with _main_loop_lock:
        _main_loop = None
    with _mismatch_lock:
        _mismatch_lifetime = 0
        _mismatch_times.clear()
        _MISMATCH_HEALTH_WINDOW_S = 15 * 60.0


__all__ = [
    "capture_main_loop",
    "get_main_loop",
    "is_bridge_timeout",
    "is_event_loop_mismatch",
    "is_infra_data_failure",
    "mismatch_count",
    "mismatch_health_window_s",
    "note_event_loop_mismatch",
    "note_infra_data_failure",
    "recent_mismatch_count",
    "reset_for_tests",
    "run_coro_sync",
    "set_mismatch_health_window_for_tests",
]
