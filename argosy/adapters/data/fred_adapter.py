"""FRED adapter (Phase 2).

Wraps the `fredapi` library for macro series (rates, FX, inflation,
ISM/PMI). Reads its API key via `argosy.secrets.get_secret(...)` first,
env var (`FRED_API_KEY`) fallback. Cached per SDD §8.3 (6h TTL).

Tests inject a fake `client` (any object exposing `get_series(series_id,
observation_start=..., observation_end=...)` returning a sequence of
date/value pairs).
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from argosy.adapters import MissingAPIKeyError, MissingDataSourceError
from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.secrets import get_secret

KEYCHAIN_KEY = "argosy.fred.api_key"
ENV_VAR = "FRED_API_KEY"


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
        provider="FRED", keychain_key=KEYCHAIN_KEY, env_var=ENV_VAR
    )


class FredAdapter:
    """FRED wrapper. Cached. Inject `client` in tests.

    `client` is an object with `get_series(series_id, observation_start=date,
    observation_end=date)` returning list-of-(date, value) tuples or a
    pandas Series. In tests pass a SimpleNamespace.
    """

    PROVIDER = "fred"

    def __init__(self, *, client: Any | None = None, api_key: str | None = None) -> None:
        self._client = client
        self._api_key = api_key

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from fredapi import Fred  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MissingDataSourceError(
                "fredapi package is not installed. Run: uv add fredapi"
            ) from exc
        api_key = self._api_key or _resolve_api_key()
        self._client = Fred(api_key=api_key)
        return self._client

    async def get_series(
        self,
        series_id: str,
        *,
        start: date | None = None,
        end: date | None = None,
        ttl_seconds: int = 60 * 60 * 6,  # SDD §8.3: 6h
    ) -> list[dict[str, Any]]:
        """Return list of {'date', 'value'} for a FRED series."""
        client = self._resolve_client()
        s = (start.isoformat() if start else "")
        e = (end.isoformat() if end else "")
        key = f"series:{series_id}:{s}:{e}"

        def _fetch() -> list[dict[str, Any]]:
            kwargs: dict[str, Any] = {}
            if start is not None:
                kwargs["observation_start"] = start
            if end is not None:
                kwargs["observation_end"] = end
            data = client.get_series(series_id, **kwargs)
            rows: list[dict[str, Any]] = []
            if data is None:
                return rows
            if isinstance(data, list):
                # Test-style: list of (date, value) tuples or list of dicts.
                for item in data:
                    if isinstance(item, dict):
                        rows.append(item)
                    else:
                        d, v = item
                        rows.append(
                            {
                                "date": d.isoformat() if hasattr(d, "isoformat") else str(d),
                                "value": float(v) if v is not None else None,
                            }
                        )
                return rows
            try:
                # pandas Series indexed by Timestamp.
                for idx, val in data.items():
                    rows.append(
                        {
                            "date": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
                            "value": float(val) if val == val and val is not None else None,  # NaN→None
                        }
                    )
            except Exception:  # pragma: no cover - defensive
                return []
            return rows

        return await cached_call(
            kind=CacheKind.MACRO,
            provider=self.PROVIDER,
            key=key,
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )


__all__ = ["FredAdapter"]
