"""yfinance adapter (Phase 2).

Wraps the `yfinance` package for EOD prices and quotes. yfinance does
not require an API key; absence of the package itself is the only
"missing source" mode. The adapter caches results in `prices_cache` per
SDD §8.3.

Tests inject a fake yfinance module via the `client` constructor arg so
no live calls happen. Production callers leave `client=None` and the
adapter imports `yfinance` lazily.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.cache import CacheKind, cached_call


@dataclass
class Quote:
    ticker: str
    price: float | None
    currency: str | None = None
    timestamp_utc: str | None = None  # ISO 8601


class YFinanceAdapter:
    """yfinance wrapper. Caching-aware.

    `client` is injectable for tests; it's a module-like object exposing
    `Ticker(symbol)` returning an object with `.history(...)`,
    `.fast_info`, `.info` etc. In tests, pass a SimpleNamespace.
    """

    PROVIDER = "yfinance"

    def __init__(self, *, client: Any | None = None) -> None:
        self._client = client

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import yfinance  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MissingDataSourceError(
                "yfinance package is not installed. Run: uv add yfinance"
            ) from exc
        self._client = yfinance
        return self._client

    async def get_eod_prices(
        self,
        tickers: list[str],
        start: date,
        end: date,
        *,
        ttl_seconds: int = 60 * 60 * 6,  # SDD §8.3: ~EOD-only after close
    ) -> dict[str, list[dict[str, Any]]]:
        """Return per-ticker list of OHLC bars between [start, end]. Cached.

        The shape returned matches what `yfinance.Ticker(t).history(...)`
        gives, normalized to a list of dicts (Date, Open, High, Low, Close,
        Volume). Empty list for tickers that returned no data.
        """
        client = self._resolve_client()
        out: dict[str, list[dict[str, Any]]] = {}
        for ticker in tickers:
            key = f"eod:{ticker}:{start.isoformat()}:{end.isoformat()}"

            def _fetch(t: str = ticker) -> list[dict[str, Any]]:
                tk = client.Ticker(t)
                hist = tk.history(start=start.isoformat(), end=end.isoformat())
                # Normalize pandas DataFrame to list-of-dict if present.
                rows: list[dict[str, Any]] = []
                if hist is None:
                    return rows
                # Duck-typed: tests can return a list directly.
                if isinstance(hist, list):
                    return hist
                try:
                    # pandas DataFrame
                    for idx, row in hist.iterrows():
                        rows.append(
                            {
                                "Date": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
                                "Open": float(row.get("Open", 0) or 0),
                                "High": float(row.get("High", 0) or 0),
                                "Low": float(row.get("Low", 0) or 0),
                                "Close": float(row.get("Close", 0) or 0),
                                "Volume": float(row.get("Volume", 0) or 0),
                            }
                        )
                except Exception:  # pragma: no cover - defensive
                    return []
                return rows

            payload = await cached_call(
                kind=CacheKind.PRICES,
                provider=self.PROVIDER,
                key=key,
                ttl_seconds=ttl_seconds,
                fetch=_fetch,
            )
            out[ticker] = payload
        return out

    async def get_quote(
        self, ticker: str, *, ttl_seconds: int = 300
    ) -> Quote:
        """Latest quote (typically last close)."""
        client = self._resolve_client()
        key = f"quote:{ticker}"

        def _fetch() -> dict[str, Any]:
            tk = client.Ticker(ticker)
            info = getattr(tk, "fast_info", None)
            if info is not None:
                price = (
                    getattr(info, "last_price", None)
                    or getattr(info, "lastPrice", None)
                )
                currency = getattr(info, "currency", None)
                if price is not None:
                    return {
                        "ticker": ticker,
                        "price": float(price),
                        "currency": currency,
                        "timestamp_utc": None,
                    }
            # Fallback: tk.info dictionary (some yfinance versions).
            info_dict: dict[str, Any] = getattr(tk, "info", {}) or {}
            price = info_dict.get("regularMarketPrice") or info_dict.get("currentPrice")
            return {
                "ticker": ticker,
                "price": float(price) if price is not None else None,
                "currency": info_dict.get("currency"),
                "timestamp_utc": None,
            }

        payload = await cached_call(
            kind=CacheKind.PRICES,
            provider=self.PROVIDER,
            key=key,
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )
        return Quote(**payload)


__all__ = ["Quote", "YFinanceAdapter"]
