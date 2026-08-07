"""Provider-specific ticker normalization for class-share symbols.

Internal / portfolio symbols use a SLASH for class shares (e.g. ``BRK/B``).
Providers disagree on the separator:

* Yahoo / yfinance wants a DASH -> ``BRK-B``
* finnhub wants a DOT           -> ``BRK.B``

A slash-only ticker sent verbatim breaks the Yahoo ``quoteSummary/BRK/B``
URL path (the slash becomes a path segment) and makes finnhub return empty
metrics. A plain ``/`` replace is a no-op on the Hebrew / dash TA-200 tickers
(``ת"א-200``), so non-US rows are never altered.
"""
from __future__ import annotations


def to_yahoo_symbol(symbol: str) -> str:
    """Portfolio symbol -> Yahoo/yfinance form (BRK/B -> BRK-B). No-op on
    plain tickers and on non-latin (Hebrew) tickers."""
    if not symbol:
        return symbol
    return symbol.strip().upper().replace("/", "-").replace(".", "-")


def to_finnhub_symbol(symbol: str) -> str:
    """Portfolio symbol -> finnhub form (BRK/B -> BRK.B). No-op on plain
    tickers and on non-latin (Hebrew) tickers."""
    if not symbol:
        return symbol
    return symbol.strip().replace("/", ".")


__all__ = ["to_yahoo_symbol", "to_finnhub_symbol"]
