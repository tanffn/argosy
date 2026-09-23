from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace

import pytest
from yfinance.exceptions import YFRateLimitError

from argosy.adapters.data.yahoo_access import YahooAccess, YahooCooldown, YahooUnavailable
from argosy.adapters.data.yfinance_adapter import YFinanceAdapter


def test_parallel_callers_stop_after_rate_limit_and_restart_preserves_it(tmp_path):
    calls = []

    def upstream():
        calls.append(1)
        raise YFRateLimitError()

    def caller(_):
        with pytest.raises(YahooCooldown):
            YahooAccess(tmp_path).run(upstream)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(caller, range(24)))
    assert len(calls) == 1
    # A genuinely separate interpreter sees the same state, not a module flag.
    code = "from pathlib import Path; import sys; from argosy.adapters.data.yahoo_access import YahooAccess,YahooCooldown;\ntry: YahooAccess(Path(sys.argv[1])).check()\nexcept YahooCooldown: print('cooldown preserved'); sys.exit(0)\nsys.exit(1)"
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True,
                            text=True, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert result.returncode == 0, result.stderr
    assert "cooldown preserved" in result.stdout


def test_retry_window_backoff_and_successful_recovery(tmp_path):
    clock = [1000.0]
    guard = YahooAccess(tmp_path, clock=lambda: clock[0])

    def fail():
        raise YFRateLimitError()

    for seconds in (900, 1800, 3600):
        with pytest.raises(YahooCooldown) as error:
            guard.run(fail)
        assert error.value.retry_at == clock[0] + seconds
        clock[0] = error.value.retry_at + 1
    assert guard.run(lambda: {"price": 123}) == {"price": 123}
    guard.check()
    with pytest.raises(YahooCooldown) as error:
        guard.run(fail)
    assert error.value.retry_at == clock[0] + 900


def test_os_lease_excludes_other_process_and_releases_after_error(tmp_path):
    guard = YahooAccess(tmp_path)
    code = "from pathlib import Path; import sys; from argosy.adapters.data.yahoo_access import YahooAccess,YahooUnavailable;\ntry: YahooAccess(Path(sys.argv[1]),wait_seconds=0).run(lambda:print('unexpected network'))\nexcept YahooUnavailable: print('lease held');sys.exit(0)\nsys.exit(1)"
    with guard._lease():
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True,
                                text=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "lease held"
    with pytest.raises(ValueError):
        guard.run(lambda: int("invalid"))
    assert guard.run(lambda: 123) == 123
    assert guard.status()["sdk_operations"] == 2


def test_busy_admission_is_not_a_rate_limit_or_success(tmp_path):
    guard = YahooAccess(tmp_path, wait_seconds=0)
    with guard._lease(), pytest.raises(YahooUnavailable, match="already in progress"):
        guard.run(lambda: pytest.fail("must not reach upstream"))
    assert guard.status()["sdk_operations"] == 0
    assert guard.status()["retry_at"] is None


def test_cancellation_during_cache_recheck_never_counts_or_starts_sdk(tmp_path):
    import threading

    cancelled = threading.Event()
    guard = YahooAccess(tmp_path)

    def recheck():
        cancelled.set()

    with pytest.raises(YahooUnavailable, match="cancelled"):
        guard.run(lambda: pytest.fail("Must not start network"),
                  recheck=recheck, cancelled=cancelled.is_set)
    assert guard.status()["sdk_operations"] == 0


@pytest.mark.asyncio
async def test_cached_quote_survives_cooldown_and_all_adapter_misses_share_it(engine, tmp_path):
    calls = []
    fail = [False]

    class Ticker:
        @property
        def fast_info(self):
            if fail[0]:
                raise YFRateLimitError()
            return SimpleNamespace(last_price=123, currency="USD")

    def ticker(symbol):
        calls.append(symbol)
        return Ticker()

    adapter = YFinanceAdapter(client=SimpleNamespace(Ticker=ticker), access=YahooAccess(tmp_path))
    first = await adapter.get_quote("BRK/B")
    assert first.ticker == "BRK/B" and calls == ["BRK-B"]
    fail[0] = True
    with pytest.raises(YahooCooldown):
        await adapter.get_quote("NVDA")
    assert (await adapter.get_quote("BRK/B")).price == 123
    for operation in (
        adapter.get_quote("META"),
        adapter.get_quote_with_fundamentals("SOFI"),
        adapter.get_eod_prices(["SPY"], date(2026, 1, 1), date(2026, 1, 2)),
        adapter.get_indicators("AAPL"),
    ):
        with pytest.raises(YahooCooldown):
            await operation
    assert calls == ["BRK-B", "NVDA"]


@pytest.mark.asyncio
async def test_blocking_sdk_call_does_not_block_event_loop(engine, tmp_path):
    import threading

    started, release = threading.Event(), threading.Event()

    def ticker(_):
        started.set()
        assert release.wait(5)
        return SimpleNamespace(fast_info=SimpleNamespace(last_price=100, currency="USD"))

    adapter = YFinanceAdapter(client=SimpleNamespace(Ticker=ticker), access=YahooAccess(tmp_path))
    task = asyncio.create_task(adapter.get_quote("AAPL"))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        await asyncio.sleep(0)  # event loop continues while provider is blocked
    finally:
        release.set()
    assert (await task).price == 100


@pytest.mark.asyncio
async def test_cancelled_admission_never_calls_sdk(engine, tmp_path):
    import threading

    entered, finished = threading.Event(), threading.Event()

    class ObservedAccess(YahooAccess):
        def run(self, *args, **kwargs):
            entered.set()
            try:
                return super().run(*args, **kwargs)
            finally:
                finished.set()

    def network(_):
        pytest.fail("Cancelled admission must not invoke SDK")

    guard = ObservedAccess(tmp_path)
    adapter = YFinanceAdapter(client=SimpleNamespace(Ticker=network), access=guard)
    with YahooAccess(tmp_path)._lease():
        task = asyncio.create_task(adapter.get_quote("AAPL"))
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(finished.wait, 3)
    assert guard.status()["sdk_operations"] == 0


@pytest.mark.asyncio
async def test_concurrent_success_rechecks_cache_and_persists_one_row(engine, tmp_path):
    from sqlalchemy import select

    from argosy.state import db as db_mod
    from argosy.state.models import KvCacheEntry

    calls = []

    def ticker(symbol):
        calls.append(symbol)
        return SimpleNamespace(fast_info=SimpleNamespace(last_price=123, currency="USD"))

    adapter = YFinanceAdapter(client=SimpleNamespace(Ticker=ticker), access=YahooAccess(tmp_path))
    results = await asyncio.gather(*(adapter.get_quote("AAPL") for _ in range(12)))
    assert all(result.price == 123 for result in results)
    assert len(calls) < len(results)  # Admission recheck avoids redundant SDK I/O.
    async with db_mod.get_session() as session:
        rows = (await session.execute(select(KvCacheEntry).where(
            KvCacheEntry.provider == "yfinance", KvCacheEntry.key == "quote:AAPL",
        ))).scalars().all()
        assert len(rows) == 1
