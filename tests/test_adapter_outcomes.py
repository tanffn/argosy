"""Tests for the adapter-outcome contextvar (T0.2)."""
from __future__ import annotations

import asyncio
import contextvars

import pytest

from argosy.services.adapter_outcomes import (
    AdapterOutcome,
    collect_outcomes,
    reset_outcomes,
    track_adapter_call,
)


@pytest.mark.asyncio
async def test_track_adapter_call_records_success():
    reset_outcomes()
    with track_adapter_call("finnhub_news", target="NVDA") as ctx:
        ctx.set_payload_size_bytes(2048)
    outcomes = collect_outcomes()
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.adapter_name == "finnhub_news"
    assert o.target == "NVDA"
    assert o.status == "ok"
    assert o.payload_size_bytes == 2048
    assert o.error_text is None
    assert o.http_status_code is None
    assert o.latency_ms >= 0


@pytest.mark.asyncio
async def test_track_adapter_call_records_http_error():
    reset_outcomes()
    with track_adapter_call("sec_13f", target="13F-HR") as ctx:
        ctx.record_http_error(status_code=404, body="Not Found")
    outcomes = collect_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0].status == "http_error"
    assert outcomes[0].http_status_code == 404
    assert outcomes[0].error_text is not None
    assert "Not Found" in outcomes[0].error_text


@pytest.mark.asyncio
async def test_track_adapter_call_records_empty_payload():
    reset_outcomes()
    with track_adapter_call("tipranks", target="NVDA") as ctx:
        ctx.set_payload_size_bytes(0)
    outcomes = collect_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0].status == "empty"
    assert outcomes[0].payload_size_bytes == 0


@pytest.mark.asyncio
async def test_track_adapter_call_records_default_empty_when_nothing_set():
    """If neither payload nor explicit status is set, status defaults to 'empty'."""
    reset_outcomes()
    with track_adapter_call("yfinance", target="^GSPC"):
        pass
    outcomes = collect_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0].status == "empty"


@pytest.mark.asyncio
async def test_track_adapter_call_records_exception_and_reraises():
    """Body-raised exceptions are recorded as status='exception' and re-raised."""
    reset_outcomes()

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with track_adapter_call("boi", target="rates"):
            raise _Boom("network down")

    outcomes = collect_outcomes()
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.status == "exception"
    assert o.adapter_name == "boi"
    assert o.target == "rates"
    assert o.error_text is not None
    assert "_Boom" in o.error_text
    assert "network down" in o.error_text


@pytest.mark.asyncio
async def test_explicit_http_error_wins_over_payload_size_heuristic():
    """An explicit record_http_error must override payload-based ok/empty inference."""
    reset_outcomes()
    with track_adapter_call("fred", target="DCOILWTICO") as ctx:
        ctx.set_payload_size_bytes(1234)  # would normally be "ok"
        ctx.record_http_error(status_code=500, body=None)
    outcomes = collect_outcomes()
    assert outcomes[0].status == "http_error"
    assert outcomes[0].http_status_code == 500
    # body=None → fall back to "HTTP 500" sentinel.
    assert outcomes[0].error_text == "HTTP 500"


@pytest.mark.asyncio
async def test_reset_outcomes_safe_when_no_outcomes_yet():
    """reset_outcomes() must not raise on a fresh context with no prior pushes."""
    # Run in a brand-new context where _outcomes default is None.
    ctx = contextvars.copy_context()

    def _inner() -> list[AdapterOutcome]:
        reset_outcomes()  # must not raise
        return collect_outcomes()

    result = ctx.run(_inner)
    assert result == []


@pytest.mark.asyncio
async def test_collect_outcomes_returns_empty_list_when_unset():
    """collect_outcomes() returns [] when no reset/push has happened."""
    ctx = contextvars.copy_context()
    result = ctx.run(collect_outcomes)
    assert result == []


@pytest.mark.asyncio
async def test_multiple_outcomes_preserve_order():
    reset_outcomes()
    with track_adapter_call("a") as ctx:
        ctx.set_payload_size_bytes(10)
    with track_adapter_call("b") as ctx:
        ctx.set_payload_size_bytes(0)
    with track_adapter_call("c") as ctx:
        ctx.record_http_error(status_code=429, body="rate limited")
    outcomes = collect_outcomes()
    assert [o.adapter_name for o in outcomes] == ["a", "b", "c"]
    assert [o.status for o in outcomes] == ["ok", "empty", "http_error"]


@pytest.mark.asyncio
async def test_collect_outcomes_is_non_destructive():
    reset_outcomes()
    with track_adapter_call("x") as ctx:
        ctx.set_payload_size_bytes(5)
    first = collect_outcomes()
    second = collect_outcomes()
    assert len(first) == 1
    assert len(second) == 1
    # Returns a copy — mutating it must not affect the buffer.
    first.clear()
    third = collect_outcomes()
    assert len(third) == 1


@pytest.mark.asyncio
async def test_contextvar_isolation_across_asyncio_tasks():
    """Each asyncio task gets its own copy of the contextvar buffer.

    This is the whole reason we use ContextVar instead of a module-level
    list: two concurrent synthesis runs must not stomp on each other.
    """

    async def _worker(name: str, n: int) -> list[AdapterOutcome]:
        # Each task runs in its own Context (asyncio.create_task copies parent ctx),
        # so reset_outcomes() here only affects this task.
        reset_outcomes()
        for i in range(n):
            with track_adapter_call(name, target=f"t{i}") as ctx:
                ctx.set_payload_size_bytes(100 + i)
                # Yield control so tasks actually interleave.
                await asyncio.sleep(0)
        return collect_outcomes()

    results = await asyncio.gather(
        _worker("alpha", 3),
        _worker("beta", 2),
        _worker("gamma", 4),
    )

    alpha, beta, gamma = results
    assert [o.adapter_name for o in alpha] == ["alpha"] * 3
    assert [o.adapter_name for o in beta] == ["beta"] * 2
    assert [o.adapter_name for o in gamma] == ["gamma"] * 4
    # Targets are per-task too — no cross-contamination.
    assert [o.target for o in alpha] == ["t0", "t1", "t2"]
    assert [o.target for o in beta] == ["t0", "t1"]


@pytest.mark.asyncio
async def test_target_is_optional():
    reset_outcomes()
    with track_adapter_call("no_target_adapter") as ctx:
        ctx.set_payload_size_bytes(1)
    outcomes = collect_outcomes()
    assert outcomes[0].target is None
    assert outcomes[0].status == "ok"


@pytest.mark.asyncio
async def test_latency_ms_is_recorded():
    reset_outcomes()
    with track_adapter_call("slowpoke") as ctx:
        await asyncio.sleep(0.01)  # 10 ms
        ctx.set_payload_size_bytes(1)
    outcomes = collect_outcomes()
    assert outcomes[0].latency_ms >= 5  # generous lower bound for CI jitter
