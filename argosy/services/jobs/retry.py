"""Bounded transient-only retry helper (Spec A §1.8).

The registry doesn't itself wrap tick bodies in retries — individual
jobs that want retry semantics call :func:`retry_transient` around the
transport call that can blip. v1 ships one retry with a uniform-random
jitter sleep of 0.5-2.0 seconds.

Transport / timeout errors retried::

    aiohttp.ClientError
    asyncio.TimeoutError
    httpx.TransportError
    anthropic.APIConnectionError
    anthropic.APITimeoutError

Business-rule errors (IntegrityError, ValidationError, schema mismatch)
and LLM-content errors (Anthropic BadRequestError / RateLimitError)
hard-fail without retry — see §1.8 of the spec for the rationale.

Imports are guarded: each library is optional and the retryable-tuple
is built lazily so the registry shell still imports cleanly on a
machine without (e.g.) aiohttp installed. Defensive — codex
``--sandbox`` envs sometimes lack a subset.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from argosy.logging import get_logger

_log = get_logger("argosy.jobs.retry")


T = TypeVar("T")


def _transient_exception_types() -> tuple[type[BaseException], ...]:
    """Build the retryable-exception tuple lazily.

    Imports are guarded with try/except so a missing optional dep
    (aiohttp / httpx / anthropic) doesn't break the registry shell.
    ``asyncio.TimeoutError`` is always present.
    """
    types: list[type[BaseException]] = [asyncio.TimeoutError]
    try:
        import aiohttp  # type: ignore[import-not-found]

        types.append(aiohttp.ClientError)
    except ImportError:  # pragma: no cover - aiohttp is a direct dep
        pass
    try:
        import httpx  # type: ignore[import-not-found]

        types.append(httpx.TransportError)
    except ImportError:  # pragma: no cover - httpx is a direct dep
        pass
    try:
        import anthropic  # type: ignore[import-not-found]

        # APIConnectionError covers DNS / TCP / TLS blips; APITimeoutError
        # covers per-request timeouts. Rate-limit / bad-request are NOT
        # transient (see module docstring).
        api_conn = getattr(anthropic, "APIConnectionError", None)
        api_timeout = getattr(anthropic, "APITimeoutError", None)
        if api_conn is not None:
            types.append(api_conn)
        if api_timeout is not None:
            types.append(api_timeout)
    except ImportError:  # pragma: no cover - anthropic is a direct dep
        pass
    return tuple(types)


# Lazily resolved on first :func:`retry_transient` call. Computing at
# module-import time was a footgun for tests that install a stub
# library AFTER import — those stubs' exception classes would never
# match the cached tuple. Computing per-call adds a tiny dict-lookup
# cost; tests that need to override it call
# :func:`_set_transient_types_for_tests`.
_TRANSIENT_TYPES: tuple[type[BaseException], ...] | None = None


def _set_transient_types_for_tests(
    types: tuple[type[BaseException], ...] | None,
) -> None:
    """Override the retryable-exception tuple. Tests-only seam.

    Pass ``None`` to reset to the lazy-resolved default.
    """
    global _TRANSIENT_TYPES
    _TRANSIENT_TYPES = types


def _get_transient_types() -> tuple[type[BaseException], ...]:
    global _TRANSIENT_TYPES
    if _TRANSIENT_TYPES is None:
        _TRANSIENT_TYPES = _transient_exception_types()
    return _TRANSIENT_TYPES


@dataclass(frozen=True)
class RetryConfig:
    """Per-job retry knob.

    ``attempts`` is the total number of attempts INCLUDING the first
    call. ``attempts=2`` = "try once + retry once on transient error";
    ``attempts=1`` = "no retry" (the v1 default for almost everything).
    """

    attempts: int = 2
    jitter_min_s: float = 0.5
    jitter_max_s: float = 2.0

    #: Sentinel value: no retries at all (business-only jobs).
    @classmethod
    def no_retry(cls) -> "RetryConfig":
        return cls(attempts=1)


#: Default for callers who don't override. One retry with jitter
#: 0.5-2.0s. Module attribute so ``RetryConfig.DEFAULT`` works the way
#: spec §1.8 documents it.
RetryConfig.DEFAULT = RetryConfig()  # type: ignore[attr-defined]


async def retry_transient(
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int = 2,
    jitter_min_s: float = 0.5,
    jitter_max_s: float = 2.0,
    job_name: str | None = None,
) -> T:
    """Call ``func()`` and retry on transport-layer errors only.

    Parameters
    ----------
    func:
        Zero-arg coroutine factory. Called fresh on each attempt so the
        underlying request gets a new socket / new state.
    attempts:
        Total attempts including the first call. Default 2 = one retry.
    jitter_min_s / jitter_max_s:
        Uniform random sleep range between attempts. Defaults match
        spec §1.8 (0.5-2.0s).
    job_name:
        Optional label for the retry log line. The registry passes
        the job's name through so operators can correlate.

    Business-rule + LLM-content exceptions propagate immediately —
    only the types in ``_TRANSIENT_TYPES`` are retried. ``CancelledError``
    is always re-raised without sleep.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    transient_types = _get_transient_types()
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — narrow check below
            if not isinstance(exc, transient_types):
                # Business-rule / LLM-content errors hard-fail; do NOT
                # retry, do NOT swallow.
                raise
            last_exc = exc
            if attempt >= attempts:
                # Exhausted retries; re-raise the last transient.
                _log.warning(
                    "jobs.retry.exhausted",
                    job=job_name,
                    attempts=attempts,
                    exc_type=type(exc).__name__,
                    error=str(exc),
                )
                raise
            sleep_s = random.uniform(jitter_min_s, jitter_max_s)
            _log.info(
                "jobs.retry.transient",
                job=job_name,
                attempt=attempt,
                next_sleep_s=round(sleep_s, 3),
                exc_type=type(exc).__name__,
                error=str(exc),
            )
            await asyncio.sleep(sleep_s)
    # Unreachable — loop either returns or raises.
    assert last_exc is not None
    raise last_exc


__all__ = ["RetryConfig", "retry_transient"]
