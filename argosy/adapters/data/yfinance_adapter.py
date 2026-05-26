"""yfinance adapter (Phase 2).

Wraps the `yfinance` package for EOD prices and quotes. yfinance does
not require an API key; absence of the package itself is the only
"missing source" mode. The adapter caches results in `kv_cache` per
SDD §8.3 (using ``CacheKind.PRICES`` for the namespace).

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
from argosy.services.adapter_outcomes import track_adapter_call


def _approx_size_bytes(payload: Any) -> int:
    """Cheap size estimate for adapter-outcome tracking."""
    import json as _json

    try:
        return len(_json.dumps(payload, default=str))
    except (TypeError, ValueError):
        return 0


@dataclass
class Quote:
    ticker: str
    price: float | None
    currency: str | None = None
    timestamp_utc: str | None = None  # ISO 8601


def _sma(series: list[float], window: int) -> float | None:
    """Simple moving average of the last ``window`` values; None if short."""
    if len(series) < window or window <= 0:
        return None
    window_slice = series[-window:]
    return sum(window_slice) / float(window)


def _ema_series(series: list[float], window: int) -> list[float]:
    """Exponential moving average over ``series``; returns same length as
    input (with the first ``window-1`` entries as the seeded SMA so the
    first valid output is at index ``window-1``)."""
    if not series or window <= 0:
        return []
    if len(series) < window:
        return []
    k = 2.0 / (window + 1.0)
    out: list[float] = []
    # Seed with SMA of first `window` points, then EMA forward.
    seed = sum(series[:window]) / float(window)
    out.append(seed)
    for value in series[window:]:
        out.append(value * k + out[-1] * (1.0 - k))
    return out


def _rsi(closes: list[float], window: int = 14) -> float | None:
    """Wilder-style RSI on the last ``window`` deltas. None if insufficient."""
    if len(closes) <= window:
        return None
    gains = 0.0
    losses = 0.0
    # Average the most recent `window` deltas.
    for i in range(len(closes) - window, len(closes)):
        delta = closes[i] - closes[i - 1]
        if delta > 0:
            gains += delta
        else:
            losses -= delta  # losses positive
    avg_gain = gains / window
    avg_loss = losses / window
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else None
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    window: int = 14,
) -> float | None:
    """Average True Range over the last ``window`` bars. None if short."""
    n = min(len(highs), len(lows), len(closes))
    if n <= window:
        return None
    trs: list[float] = []
    for i in range(n - window, n):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        trs.append(tr)
    return sum(trs) / float(window)


def _compute_indicators(
    *,
    ticker: str,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
) -> dict[str, Any]:
    """Pure helper: derive the indicator dict from OHLC series."""
    price = closes[-1] if closes else None
    ma_50 = _sma(closes, 50)
    ma_200 = _sma(closes, 200)
    # Golden/death cross: today's relation between MA50 and MA200 vs
    # yesterday's. None if either MA isn't computable today/yesterday.
    cross = "none"
    if len(closes) >= 201:
        prev_ma_50 = _sma(closes[:-1], 50)
        prev_ma_200 = _sma(closes[:-1], 200)
        if (
            ma_50 is not None and ma_200 is not None
            and prev_ma_50 is not None and prev_ma_200 is not None
        ):
            if prev_ma_50 <= prev_ma_200 and ma_50 > ma_200:
                cross = "golden"
            elif prev_ma_50 >= prev_ma_200 and ma_50 < ma_200:
                cross = "death"
    rsi_14 = _rsi(closes, 14)
    # MACD: 12-EMA minus 26-EMA, signal = 9-EMA of the MACD line.
    macd_val: float | None = None
    macd_signal: float | None = None
    if len(closes) >= 35:
        ema_12 = _ema_series(closes, 12)
        ema_26 = _ema_series(closes, 26)
        # Align EMAs by trimming the longer one's prefix.
        if ema_12 and ema_26:
            offset = len(ema_12) - len(ema_26)
            if offset > 0:
                ema_12 = ema_12[offset:]
            macd_line = [a - b for a, b in zip(ema_12, ema_26, strict=False)]
            if macd_line:
                macd_val = macd_line[-1]
                if len(macd_line) >= 9:
                    sig_series = _ema_series(macd_line, 9)
                    if sig_series:
                        macd_signal = sig_series[-1]
    atr_14 = _atr(highs, lows, closes, 14)
    # Naive support/resistance: last-60-bar low / high (3 trading months).
    tail_60 = closes[-60:] if len(closes) >= 60 else closes
    support = min(tail_60) if tail_60 else None
    resistance = max(tail_60) if tail_60 else None
    # 252-trading-day window for the 52w range.
    tail_252 = closes[-252:] if len(closes) >= 252 else closes
    w52_high = max(tail_252) if tail_252 else None
    w52_low = min(tail_252) if tail_252 else None
    vol_tail = volumes[-20:] if len(volumes) >= 20 else volumes
    volume_avg = (sum(vol_tail) / float(len(vol_tail))) if vol_tail else None
    # Drop None values entirely — TechnicalReport's pydantic schema requires
    # non-null floats per indicator, and live run #19 hit 18 validation
    # errors because ma_200 was None for tickers with <200 trading days of
    # history. The agent treats missing keys gracefully ("no ma_200 in
    # input"); it does NOT tolerate explicit None values.
    raw: dict[str, Any] = {
        "price": price,
        "rsi_14": rsi_14,
        "macd": macd_val,
        "macd_signal": macd_signal,
        "ma_50": ma_50,
        "ma_200": ma_200,
        "ma_cross_50_200": cross,
        "atr_14": atr_14,
        "support": support,
        "resistance": resistance,
        "52w_high": w52_high,
        "52w_low": w52_low,
        "volume_avg": volume_avg,
    }
    out: dict[str, Any] = {
        k: float(v) for k, v in raw.items() if v is not None and k != "ma_cross_50_200"
    }
    if cross is not None:
        out["ma_cross_50_200"] = cross
    out["source"] = f"yfinance:{ticker}:1d"
    return out


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

    async def get_indicators(
        self,
        ticker: str,
        *,
        ttl_seconds: int = 60 * 60 * 6,  # SDD §8.3: ~EOD refresh
    ) -> dict[str, Any]:
        """Return pre-computed technical indicators for ``ticker``.

        Pulls ~6 months of OHLC via ``yfinance.Ticker(t).history(period="6mo")``
        and computes the indicators advertised by
        :py:meth:`argosy.agents.technical_analyst.TechnicalAnalystAgent.build_prompt`:
        ``rsi_14``, ``macd``, ``macd_signal``, ``ma_50``, ``ma_200``,
        ``ma_cross_50_200`` (``"golden" | "death" | "none"``), ``atr_14``,
        ``support``, ``resistance``, plus ``price``, ``52w_high``,
        ``52w_low``, ``volume_avg`` and a ``source`` string of the form
        ``yfinance:<TICKER>:1d``.

        Raises ``MissingDataSourceError`` if the ``yfinance`` package is
        not installed or the upstream call returned no history.
        Per-indicator values that can't be computed (e.g. ``ma_200`` on
        <200 bars) are returned as ``None``.
        """
        with track_adapter_call("yfinance_indicators", target=ticker) as _outcome:
            client = self._resolve_client()
            key = f"indicators:{ticker}"

            def _fetch() -> dict[str, Any]:
                tk = client.Ticker(ticker)
                try:
                    hist = tk.history(period="6mo", auto_adjust=True)
                except TypeError:
                    # Some test doubles only accept (start, end). Fall back to
                    # a fixed-window history call.
                    hist = tk.history(period="6mo")
                if hist is None:
                    raise MissingDataSourceError(
                        f"yfinance returned no history for {ticker}"
                    )
                # Duck-typed: a test double may return a list-of-dict.
                rows: list[dict[str, Any]]
                if isinstance(hist, list):
                    rows = list(hist)
                else:
                    rows = []
                    try:
                        for _, row in hist.iterrows():
                            rows.append(
                                {
                                    "Open": float(row.get("Open", 0) or 0),
                                    "High": float(row.get("High", 0) or 0),
                                    "Low": float(row.get("Low", 0) or 0),
                                    "Close": float(row.get("Close", 0) or 0),
                                    "Volume": float(row.get("Volume", 0) or 0),
                                }
                            )
                    except Exception as exc:  # pragma: no cover - defensive
                        raise MissingDataSourceError(
                            f"yfinance history for {ticker} unreadable: {exc}"
                        ) from exc
                if not rows:
                    raise MissingDataSourceError(
                        f"yfinance returned no history for {ticker}"
                    )

                closes = [float(r["Close"]) for r in rows]
                highs = [float(r["High"]) for r in rows]
                lows = [float(r["Low"]) for r in rows]
                volumes = [float(r["Volume"]) for r in rows]
                return _compute_indicators(
                    ticker=ticker,
                    closes=closes,
                    highs=highs,
                    lows=lows,
                    volumes=volumes,
                )

            payload = await cached_call(
                kind=CacheKind.PRICES,
                provider=self.PROVIDER,
                key=key,
                ttl_seconds=ttl_seconds,
                fetch=_fetch,
            )
            _outcome.set_payload_size_bytes(_approx_size_bytes(payload))
            return payload

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
