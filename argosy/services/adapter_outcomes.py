"""Adapter outcome tracking — record what every external data call did.

Used during synthesis so the UI can show 'finnhub_news: 14 records'
(ok) or 'sec_13f: HTTP 404' (http_error). The contextvar pattern lets
adapters report their own outcomes without threading a tracker through
every call site.
"""
from __future__ import annotations

import contextlib
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Literal

OutcomeStatus = Literal["ok", "empty", "http_error", "exception"]


@dataclass
class AdapterOutcome:
    adapter_name: str
    target: str | None
    status: OutcomeStatus
    latency_ms: int
    payload_size_bytes: int = 0
    http_status_code: int | None = None
    error_text: str | None = None


_outcomes: ContextVar[list[AdapterOutcome] | None] = ContextVar(
    "adapter_outcomes", default=None,
)


class _OutcomeBuilder:
    def __init__(self, adapter_name: str, target: str | None):
        self.adapter_name = adapter_name
        self.target = target
        self._t0 = time.monotonic()
        self._payload_size = 0
        self._http_status: int | None = None
        self._error: str | None = None
        self._explicit_status: OutcomeStatus | None = None

    def set_payload_size_bytes(self, n: int) -> None:
        self._payload_size = n

    def record_http_error(self, *, status_code: int, body: str | None) -> None:
        self._http_status = status_code
        self._error = body or f"HTTP {status_code}"
        self._explicit_status = "http_error"

    def record_exception(self, exc: BaseException) -> None:
        self._error = f"{type(exc).__name__}: {exc}"
        self._explicit_status = "exception"

    def _finalize(self) -> AdapterOutcome:
        status: OutcomeStatus
        if self._explicit_status:
            status = self._explicit_status
        elif self._payload_size == 0:
            status = "empty"
        else:
            status = "ok"
        return AdapterOutcome(
            adapter_name=self.adapter_name,
            target=self.target,
            status=status,
            latency_ms=int((time.monotonic() - self._t0) * 1000),
            payload_size_bytes=self._payload_size,
            http_status_code=self._http_status,
            error_text=self._error,
        )


@contextlib.contextmanager
def track_adapter_call(
    adapter_name: str, *, target: str | None = None,
) -> Iterator[_OutcomeBuilder]:
    """Record one adapter call's outcome into the contextvar.

    On exception inside the body, the outcome is recorded with
    status="exception" *before* the exception is re-raised, so callers
    that observe outcomes after a failure still see the failed call.
    """
    builder = _OutcomeBuilder(adapter_name=adapter_name, target=target)
    try:
        yield builder
    except BaseException as exc:
        builder.record_exception(exc)
        _push(builder._finalize())
        raise
    else:
        _push(builder._finalize())


def _push(outcome: AdapterOutcome) -> None:
    cur = _outcomes.get()
    if cur is None:
        cur = []
        _outcomes.set(cur)
    cur.append(outcome)


def reset_outcomes() -> None:
    """Clear the buffer at the start of a synthesis run.

    Safe to call before any outcomes have been recorded.
    """
    _outcomes.set([])


def collect_outcomes() -> list[AdapterOutcome]:
    """Return everything tracked since last reset; non-destructive."""
    return list(_outcomes.get() or [])
