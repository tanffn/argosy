"""Shared transport diagnosis for safe, idempotent data reads.

No investment judgment or raw URLs/credentials belong in these receipts.
Long Retry-After values are handed back to durable scheduler recovery rather
than blocking a worker or retrying earlier than the provider requested.
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Callable, TypeVar
from urllib.error import HTTPError, URLError

T = TypeVar("T")
TRANSIENT_HTTP = frozenset({408, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class TransportFailure:
    code: str
    retryable: bool
    http_status: int | None = None
    retry_after_seconds: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def retry_after_seconds(value, *, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(str(value))
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - (now or datetime.now(UTC))).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def classify_transport_failure(exc: BaseException) -> TransportFailure:
    """Inspect typed failures (and explicit causes), never arbitrary prose."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        response = getattr(exc, "response", None)
        status = (exc.code if isinstance(exc, HTTPError) else
                  getattr(response, "status_code", None) or getattr(exc, "status", None))
        if isinstance(status, int) and status >= 400:
            headers = getattr(exc, "headers", None)
            if headers is None:
                headers = getattr(response, "headers", None)
            after = retry_after_seconds(headers.get("Retry-After")) if headers is not None else None
            code = ("authentication" if status in {401, 403} else
                    "rate_limit" if status == 429 else
                    "upstream_unavailable" if status in TRANSIENT_HTTP else "http_permanent")
            return TransportFailure(code, status in TRANSIENT_HTTP, status, after)
        if isinstance(exc, TimeoutError):
            return TransportFailure("timeout", True)
        if isinstance(exc, (URLError, ConnectionError)):
            return TransportFailure("network_transport", True)
        # Libraries are optional for standalone diagnostic tools.
        try:
            import httpx
            if isinstance(exc, httpx.TimeoutException):
                return TransportFailure("timeout", True)
            if isinstance(exc, httpx.TransportError):
                return TransportFailure("network_transport", True)
        except ImportError:
            pass
        exc = exc.__cause__
    return TransportFailure("unclassified", False)


def retry_data_read(func: Callable[[], T], *, attempts: int = 3,
                    backoff: tuple[float, ...] = (1.0, 3.0),
                    sleep: Callable[[float], None] = time.sleep,
                    max_inline_wait: float = 30.0) -> T:
    """Bounded retry around a READ only; parsers and permanent errors fail fast."""
    if attempts < 1 or any(not math.isfinite(x) or x < 0 for x in backoff):
        raise ValueError("Invalid read retry policy")
    for attempt in range(attempts):
        try:
            return func()
        except Exception as exc:
            failure = classify_transport_failure(exc)
            if not failure.retryable or attempt + 1 >= attempts:
                raise
            delay = max(backoff[min(attempt, len(backoff) - 1)] if backoff else 0,
                        failure.retry_after_seconds or 0)
            if delay > max_inline_wait:
                raise
            sleep(delay)
    raise AssertionError("unreachable")
