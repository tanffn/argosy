"""Event-as-of price helpers for fleet prediction backfill (Stream C).

Resolves a historical close on/near ``as_of`` without blocking the
decision path. Used by the backfill CLI so a default run does not
silently write zero rows.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any


def resolve_price_as_of(
    symbol: str,
    as_of: datetime | date | None,
    *,
    lookback_days: int = 7,
) -> float | None:
    """Return a close price on/before ``as_of`` (never a future print).

    Uses yfinance daily history. Returns ``None`` on any failure —
    callers must record a durable pending-entry prediction rather than
    inventing a level.
    """
    sym = (symbol or "").strip().upper().replace("/", "-")
    if not sym:
        return None
    if as_of is None:
        return None
    if isinstance(as_of, datetime):
        as_of_d = as_of.astimezone(timezone.utc).date() if as_of.tzinfo else as_of.date()
    else:
        as_of_d = as_of
    start = as_of_d - timedelta(days=max(lookback_days, 1))
    end = as_of_d + timedelta(days=1)
    try:
        import yfinance as yf  # type: ignore[import-untyped]

        hist = yf.Ticker(sym).history(
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
        )
        if hist is None or getattr(hist, "empty", True):
            return None
        # Prefer the last bar on/before as_of.
        closes = hist["Close"]
        for idx in reversed(list(closes.index)):
            try:
                bar_d = idx.date() if hasattr(idx, "date") else idx
            except Exception:  # noqa: BLE001
                continue
            if bar_d <= as_of_d:
                val = closes.loc[idx]
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return None
        return None
    except Exception:  # noqa: BLE001
        return None


def as_of_resolver_for_backfill(subject: str, as_of: Any) -> float | None:
    """Adapter matching :data:`EntryPriceAsOfResolver`."""
    return resolve_price_as_of(subject, as_of)
