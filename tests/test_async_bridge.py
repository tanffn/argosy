"""Sync-to-async adapter bridge works both outside and inside an event loop."""

from __future__ import annotations

import threading

import pytest

from argosy.async_bridge import run_async_from_sync
from argosy.services.snapshot_refresh import default_quote_fn


async def _value(value: int) -> int:
    return value


def test_bridge_without_running_loop() -> None:
    assert run_async_from_sync(lambda: _value(7)) == 7


@pytest.mark.asyncio
async def test_bridge_with_running_loop_owns_coroutine_in_worker() -> None:
    caller_thread = threading.get_ident()

    async def _thread_id() -> int:
        return threading.get_ident()

    worker_thread = run_async_from_sync(_thread_id)
    assert worker_thread != caller_thread


@pytest.mark.asyncio
async def test_snapshot_quote_provider_no_longer_drops_quotes_inside_loop(
    monkeypatch,
) -> None:
    from argosy.adapters.data import yfinance_adapter
    from argosy.adapters.data.yfinance_adapter import Quote

    class _Adapter:
        async def get_quote(self, ticker: str) -> Quote:
            return Quote(ticker=ticker, price=123.45, currency="USD")

    monkeypatch.setattr(yfinance_adapter, "YFinanceAdapter", _Adapter)
    assert default_quote_fn("NVDA", currency="USD", details="") == 123.45
