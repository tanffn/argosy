"""Finnhub adapter (Phase 2).

Wraps the `finnhub-python` package for company news + earnings calendar.
Free tier covers our Phase 2 needs (60 calls/min). Reads its API key via
`argosy.secrets.get_secret(...)` first, env var (`FINNHUB_API_KEY`)
fallback. Cached per SDD §8.3 (15min for news, 24h for calendar).

Tests inject a fake `client` exposing `company_news(symbol, _from, to)`
and `earnings_calendar(_from, to, symbol, international)`.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from argosy.adapters import MissingAPIKeyError, MissingDataSourceError
from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.secrets import get_secret

KEYCHAIN_KEY = "argosy.finnhub.api_key"
ENV_VAR = "FINNHUB_API_KEY"


def _resolve_api_key() -> str:
    try:
        v = get_secret(KEYCHAIN_KEY)
    except Exception:  # pragma: no cover - defensive
        v = None
    if v:
        return v
    env_v = os.environ.get(ENV_VAR)
    if env_v:
        return env_v
    raise MissingAPIKeyError(
        provider="Finnhub", keychain_key=KEYCHAIN_KEY, env_var=ENV_VAR
    )


class FinnhubAdapter:
    """Finnhub wrapper. Cached. Inject `client` in tests."""

    PROVIDER = "finnhub"

    def __init__(self, *, client: Any | None = None, api_key: str | None = None) -> None:
        self._client = client
        self._api_key = api_key

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import finnhub  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MissingDataSourceError(
                "finnhub-python package is not installed. Run: uv add finnhub-python"
            ) from exc
        api_key = self._api_key or _resolve_api_key()
        self._client = finnhub.Client(api_key=api_key)
        return self._client

    async def get_company_news(
        self,
        symbol: str,
        *,
        start: date,
        end: date,
        ttl_seconds: int = 60 * 15,  # SDD §8.3: 15min
    ) -> list[dict[str, Any]]:
        """Return list of headline dicts for `symbol` within [start, end]."""
        client = self._resolve_client()
        key = f"company_news:{symbol}:{start.isoformat()}:{end.isoformat()}"

        def _fetch() -> list[dict[str, Any]]:
            raw = client.company_news(symbol, _from=start.isoformat(), to=end.isoformat())
            if not raw:
                return []
            # Normalize: take the keys we care about.
            out: list[dict[str, Any]] = []
            for item in raw:
                out.append(
                    {
                        "headline": item.get("headline") or "",
                        "summary": item.get("summary") or "",
                        "url": item.get("url") or "",
                        "source": item.get("source") or "",
                        "datetime": item.get("datetime"),
                    }
                )
            return out

        return await cached_call(
            kind=CacheKind.NEWS,
            provider=self.PROVIDER,
            key=key,
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )

    async def get_earnings_calendar(
        self,
        *,
        start: date,
        end: date,
        symbol: str | None = None,
        ttl_seconds: int = 60 * 60 * 24,  # SDD §8.3: 24h fundamentals-class
    ) -> list[dict[str, Any]]:
        """Return list of earnings events between [start, end]."""
        client = self._resolve_client()
        key = f"earnings:{symbol or '*'}:{start.isoformat()}:{end.isoformat()}"

        def _fetch() -> list[dict[str, Any]]:
            raw = client.earnings_calendar(
                _from=start.isoformat(),
                to=end.isoformat(),
                symbol=symbol or "",
                international=False,
            )
            if isinstance(raw, dict):
                return list(raw.get("earningsCalendar", []) or [])
            if isinstance(raw, list):
                return raw
            return []

        return await cached_call(
            kind=CacheKind.NEWS,
            provider=self.PROVIDER,
            key=key,
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )


__all__ = ["FinnhubAdapter"]
