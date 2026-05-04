"""Data-adapter tests. Mock yfinance / FRED / finnhub clients.

Verifies cache TTL behavior and adapter shape. No live API calls.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.adapters.data.finnhub_adapter import FinnhubAdapter
from argosy.adapters.data.fred_adapter import FredAdapter
from argosy.adapters.data.yfinance_adapter import YFinanceAdapter
from argosy.state import db as db_mod
from argosy.state.models import KvCacheEntry


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeYfTicker:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.fast_info = SimpleNamespace(last_price=200.0, currency="USD")

    def history(self, start: str, end: str) -> list[dict]:
        return [
            {"Date": start, "Open": 199, "High": 201, "Low": 198, "Close": 200, "Volume": 1_000_000},
            {"Date": end, "Open": 200, "High": 202, "Low": 199, "Close": 201, "Volume": 1_100_000},
        ]


class _FakeYfModule:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def Ticker(self, symbol: str) -> _FakeYfTicker:
        self.calls.append(symbol)
        return _FakeYfTicker(symbol)


class _FakeFredClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def get_series(self, series_id: str, **kwargs):
        self.calls.append((series_id, kwargs))
        return [
            (date(2026, 1, 1), 4.55),
            (date(2026, 1, 2), 4.57),
        ]


class _FakeFinnhubClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def company_news(self, symbol: str, _from: str, to: str) -> list[dict]:
        self.calls.append(f"news:{symbol}")
        return [
            {
                "headline": f"{symbol} hits all-time high",
                "summary": "A summary",
                "url": "https://news.example/x",
                "source": "Reuters",
                "datetime": 1700000000,
            }
        ]

    def earnings_calendar(self, _from: str, to: str, symbol: str, international: bool) -> dict:
        self.calls.append(f"earn:{symbol}")
        return {
            "earningsCalendar": [
                {"symbol": symbol or "AAPL", "date": _from, "epsEstimate": 1.0}
            ]
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yfinance_adapter_caches_and_normalizes(engine: None) -> None:
    fake = _FakeYfModule()
    adapter = YFinanceAdapter(client=fake)
    out = await adapter.get_eod_prices(
        ["AAPL"], date(2026, 1, 1), date(2026, 1, 5), ttl_seconds=3600
    )
    assert "AAPL" in out
    assert len(out["AAPL"]) == 2
    # Second call hits cache; underlying client should NOT be called again.
    await adapter.get_eod_prices(
        ["AAPL"], date(2026, 1, 1), date(2026, 1, 5), ttl_seconds=3600
    )
    assert fake.calls == ["AAPL"], "second call must be served from cache"


@pytest.mark.asyncio
async def test_yfinance_get_quote(engine: None) -> None:
    fake = _FakeYfModule()
    adapter = YFinanceAdapter(client=fake)
    q = await adapter.get_quote("AAPL", ttl_seconds=60)
    assert q.ticker == "AAPL"
    assert q.price == 200.0
    assert q.currency == "USD"


@pytest.mark.asyncio
async def test_fred_adapter_returns_normalized_rows(engine: None) -> None:
    fake = _FakeFredClient()
    adapter = FredAdapter(client=fake, api_key="dummy")
    rows = await adapter.get_series(
        "DGS10", start=date(2026, 1, 1), end=date(2026, 1, 2), ttl_seconds=3600
    )
    assert len(rows) == 2
    assert rows[0]["value"] == 4.55
    assert rows[0]["date"] == "2026-01-01"
    # Cached on second call.
    rows2 = await adapter.get_series(
        "DGS10", start=date(2026, 1, 1), end=date(2026, 1, 2), ttl_seconds=3600
    )
    assert rows2 == rows
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_finnhub_adapter_company_news(engine: None) -> None:
    fake = _FakeFinnhubClient()
    adapter = FinnhubAdapter(client=fake, api_key="dummy")
    rows = await adapter.get_company_news(
        "NVDA", start=date(2026, 1, 1), end=date(2026, 1, 5), ttl_seconds=3600
    )
    assert len(rows) == 1
    assert rows[0]["headline"].startswith("NVDA")


@pytest.mark.asyncio
async def test_cache_ttl_zero_always_refetches(engine: None) -> None:
    """ttl_seconds=0 forces a fetch every time and overwrites the row."""
    counter = {"n": 0}

    def _fetch() -> dict:
        counter["n"] += 1
        return {"x": counter["n"]}

    out1 = await cached_call(
        kind=CacheKind.PRICES,
        provider="testprov",
        key="k1",
        ttl_seconds=0,
        fetch=_fetch,
    )
    out2 = await cached_call(
        kind=CacheKind.PRICES,
        provider="testprov",
        key="k1",
        ttl_seconds=0,
        fetch=_fetch,
    )
    assert out1["x"] == 1
    assert out2["x"] == 2

    # And the cache row exists with the latest payload.
    async with db_mod.get_session() as session:
        row = (
            await session.execute(
                select(KvCacheEntry).where(
                    (KvCacheEntry.provider == "testprov") & (KvCacheEntry.key == "k1")
                )
            )
        ).scalar_one()
        assert "x" in row.payload_json


@pytest.mark.asyncio
async def test_cache_ttl_honored(engine: None) -> None:
    """A long TTL means the second call returns the original payload."""
    counter = {"n": 0}

    def _fetch() -> dict:
        counter["n"] += 1
        return {"x": counter["n"]}

    out1 = await cached_call(
        kind=CacheKind.NEWS,
        provider="testprov",
        key="ttl_key",
        ttl_seconds=3600,
        fetch=_fetch,
    )
    out2 = await cached_call(
        kind=CacheKind.NEWS,
        provider="testprov",
        key="ttl_key",
        ttl_seconds=3600,
        fetch=_fetch,
    )
    assert out1 == out2
    assert counter["n"] == 1


@pytest.mark.asyncio
async def test_cache_ttl_expired_triggers_refetch(engine: None) -> None:
    """Manually expire the cache row, then expect a fresh fetch."""
    counter = {"n": 0}

    def _fetch() -> dict:
        counter["n"] += 1
        return {"x": counter["n"]}

    await cached_call(
        kind=CacheKind.MACRO,
        provider="testprov",
        key="exp_key",
        ttl_seconds=3600,
        fetch=_fetch,
    )

    # Expire the row.
    async with db_mod.get_session() as session:
        from argosy.state.models import MacroCache

        row = (
            await session.execute(
                select(MacroCache).where(
                    (MacroCache.provider == "testprov") & (MacroCache.key == "exp_key")
                )
            )
        ).scalar_one()
        row.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        await session.commit()

    out = await cached_call(
        kind=CacheKind.MACRO,
        provider="testprov",
        key="exp_key",
        ttl_seconds=3600,
        fetch=_fetch,
    )
    assert out["x"] == 2
    assert counter["n"] == 2
