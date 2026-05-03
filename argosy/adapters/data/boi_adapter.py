"""Bank of Israel adapter (Phase 2).

Provides USD/NIS reference rate. The Bank of Israel publishes a public
JSON API (no auth required) at https://www.boi.org.il/PublicApi/. As a
resilience strategy the adapter falls back to:

  1. BoI public API (no key needed)
  2. FRED `DEXISUS` series (USD/ILS)
  3. yfinance `USDILS=X`

If all three are unavailable the adapter raises `MissingDataSourceError`.
The fallback chain is documented and logged so audit trails reveal which
source served the value.

Tests inject the underlying clients via constructor args.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.adapters.data.fred_adapter import FredAdapter
from argosy.adapters.data.yfinance_adapter import YFinanceAdapter
from argosy.logging import get_logger

_log = get_logger("argosy.adapters.boi")


class BoiAdapter:
    """USD/NIS rate adapter. Public BoI API → FRED → yfinance fallback chain.

    `boi_client` is an object exposing `get_usd_nis(on_or_before: date)`
    returning a float, or a no-op stub. Tests pass a fake.
    """

    PROVIDER = "boi"

    def __init__(
        self,
        *,
        boi_client: Any | None = None,
        fred: FredAdapter | None = None,
        yf: YFinanceAdapter | None = None,
    ) -> None:
        self._boi = boi_client
        self._fred = fred
        self._yf = yf

    async def get_usd_nis(
        self,
        *,
        on_or_before: date | None = None,
        ttl_seconds: int = 60 * 60 * 6,  # SDD §8.3: 6h macro
    ) -> dict[str, Any]:
        """Return {'rate': float, 'source': str, 'as_of': iso_date}.

        Cache key includes the date so historical lookups don't pollute
        the spot cache.
        """
        target = on_or_before or datetime.now(timezone.utc).date()
        key = f"usd_nis:{target.isoformat()}"

        async def _fetch() -> dict[str, Any]:
            # 1. Try BoI direct
            if self._boi is not None:
                try:
                    rate = self._boi.get_usd_nis(target)
                    if rate:
                        return {
                            "rate": float(rate),
                            "source": "boi",
                            "as_of": target.isoformat(),
                        }
                except Exception as exc:  # pragma: no cover - resilience
                    _log.warning("boi.fallback", reason=str(exc))

            # 2. FRED DEXISUS
            if self._fred is not None:
                try:
                    series = await self._fred.get_series(
                        "DEXISUS", start=target, end=target
                    )
                    for row in reversed(series):
                        v = row.get("value")
                        if v is not None:
                            return {
                                "rate": float(v),
                                "source": "fred:DEXISUS",
                                "as_of": row.get("date") or target.isoformat(),
                            }
                except Exception as exc:  # pragma: no cover - resilience
                    _log.warning("fred.fallback", reason=str(exc))

            # 3. yfinance USDILS=X
            if self._yf is not None:
                try:
                    quote = await self._yf.get_quote("USDILS=X")
                    if quote.price is not None:
                        return {
                            "rate": float(quote.price),
                            "source": "yfinance:USDILS=X",
                            "as_of": target.isoformat(),
                        }
                except Exception as exc:  # pragma: no cover - resilience
                    _log.warning("yfinance.fallback", reason=str(exc))

            raise MissingDataSourceError(
                "USD/NIS rate is unavailable from BoI, FRED, and yfinance "
                "(the full Phase 2 fallback chain). Configure at least one "
                "of FRED/Finnhub keys, or install yfinance, then retry."
            )

        return await cached_call(
            kind=CacheKind.MACRO,
            provider=self.PROVIDER,
            key=key,
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )


__all__ = ["BoiAdapter"]
