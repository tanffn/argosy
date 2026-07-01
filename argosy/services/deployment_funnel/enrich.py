from __future__ import annotations

from typing import Protocol

from argosy.services.deployment_funnel.contracts import HistoryFeatures


class PriceProvider(Protocol):
    def quote(self, symbol: str) -> float | None: ...
    def history_high(self, symbol: str) -> float | None: ...
    def zscore(self, symbol: str) -> float | None: ...


def build_history_features(symbol: str, provider: PriceProvider) -> HistoryFeatures:
    """Deterministic price-history FEATURES. A missing live quote marks the
    candidate stale (the gates fail-closed to DEFER); features never gate."""
    last = provider.quote(symbol)
    ath = provider.history_high(symbol)
    z = provider.zscore(symbol)
    stale = last is None
    pct_below = (
        round((ath - last) / ath * 100, 2)
        if (last is not None and ath and ath > 0)
        else None
    )
    drawdown = pct_below  # single-window proxy in Increment 1
    return HistoryFeatures(
        last_price=last,
        ath=ath,
        pct_below_ath=pct_below,
        zscore_vs_window=z,
        drawdown_pct=drawdown,
        stale=stale,
    )


def news_sentiment_for(
    symbol: str, signals_by_symbol: dict[str, str]
) -> str | None:
    """Ingested NewsSignal sentiment for the symbol, or None => the trace must
    render 'no recent ingested signal' (NOT 'no news')."""
    return signals_by_symbol.get(symbol.upper())
