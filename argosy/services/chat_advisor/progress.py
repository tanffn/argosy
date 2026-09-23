"""Task-local bridge from the canonical agent fleet to durable chat receipts."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

ProgressSink = Callable[..., Awaitable[None]]


@dataclass
class ProgressContext:
    request_id: str
    sink: ProgressSink
    run_id: int | None = None


_progress_context: ContextVar[ProgressContext | None] = ContextVar(
    "chat_analysis_progress", default=None
)


@contextmanager
def bind_progress(request_id: str, sink: ProgressSink) -> Iterator[ProgressContext]:
    """Bind a durable sink for this analysis task and all child agent tasks."""
    context = ProgressContext(request_id=request_id, sink=sink)
    token = _progress_context.set(context)
    try:
        yield context
    finally:
        _progress_context.reset(token)


def set_progress_run_id(run_id: int) -> None:
    context = _progress_context.get()
    if context is not None:
        context.run_id = run_id


async def record_agent_progress(
    *, agent: str, state: str, detail: str | None = None, error: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Persist an actual agent milestone when running under chat dispatch."""
    context = _progress_context.get()
    if context is None:
        return
    dedup_key = ":".join(
        part for part in (str(context.run_id or "pending"), correlation_id, state)
        if part
    )
    await context.sink(
        request_id=context.request_id,
        run_id=context.run_id,
        agent=agent,
        state=state,
        detail=detail,
        error=error,
        dedup_key=dedup_key,
    )


__all__ = ["bind_progress", "record_agent_progress", "set_progress_run_id"]
