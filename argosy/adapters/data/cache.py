"""Adapter cache wrapper.

Stores responses in `kv_cache` / `news_cache` / `macro_cache` per SDD
§8.3. Honors per-record TTL. The cache is content-addressed: each row
carries a `payload_hash` so an audit trail can detect when a vendor
silently mutates a payload without changing its key.

`kv_cache` is a generic key/value/TTL store; `CacheKind` values namespace
rows within it (``PRICES``, ``NEWS``, ``MACRO``, ``UI`` …). The same
table also backs UI snapshots like the home-brief composition.

Usage::

    from argosy.adapters.data.cache import CacheKind, cached_call

    payload = await cached_call(
        kind=CacheKind.PRICES,
        provider="yfinance",
        key=f"eod:{ticker}",
        ttl_seconds=300,
        fetch=lambda: _yf_get_eod(ticker),
    )

`fetch` is a zero-arg callable returning a JSON-serializable object. It
is invoked only on cache miss / expiry. The wrapper handles serialization
and hashing.
"""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from sqlalchemy import select

from argosy.state import db as db_mod
from argosy.state.models import KvCacheEntry, MacroCache, NewsCache

T = TypeVar("T")


class CacheKind(str, enum.Enum):
    """Namespace within the underlying cache tables.

    ``PRICES``, ``NEWS``, ``MACRO``, and ``UI`` all share rows in
    ``kv_cache`` (the legacy ``prices_cache``); ``NewsCache`` /
    ``MacroCache`` exist separately for historical reasons but most new
    callers should pick a kind here and write to ``kv_cache``.
    """

    PRICES = "prices"
    NEWS = "news"
    MACRO = "macro"
    # UI snapshots (e.g. home-brief, composed dashboards). Lives in
    # ``kv_cache`` alongside PRICES — the kind is just a logical
    # namespace, not a physical table choice.
    UI = "ui"


_TABLE_BY_KIND = {
    CacheKind.PRICES: KvCacheEntry,
    CacheKind.NEWS: NewsCache,
    CacheKind.MACRO: MacroCache,
    CacheKind.UI: KvCacheEntry,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime is timezone-aware in UTC.

    SQLite via aiosqlite returns tz-naive datetimes from columns declared
    as `DateTime(timezone=True)`. We wrote UTC-aware datetimes in, so we
    can safely re-attach UTC on read.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _hash_payload(payload_json: str) -> str:
    h = hashlib.sha256()
    h.update(payload_json.encode("utf-8"))
    return h.hexdigest()


async def cached_call(
    *,
    kind: CacheKind,
    provider: str,
    key: str,
    ttl_seconds: int,
    fetch: Callable[[], Any] | Callable[[], Awaitable[Any]],
    now: Callable[[], datetime] = _utcnow,
) -> Any:
    """Return cached payload or call `fetch` and persist the result.

    Args:
        kind: which cache table to use.
        provider: e.g. 'yfinance', 'fred', 'finnhub', 'boi'.
        key: provider-scoped key (e.g., 'eod:AAPL:2025-01-01:2025-01-31').
        ttl_seconds: cache lifetime; 0 means always refresh.
        fetch: zero-arg fetcher returning JSON-serializable data (sync or
            async). On cache hit it is NOT called.
        now: clock; tests inject a fixed clock.

    Returns:
        The deserialized payload (the same object that `fetch` returned).
    """
    table = _TABLE_BY_KIND[kind]

    if ttl_seconds > 0:
        async with db_mod.get_session() as session:
            row = (
                await session.execute(
                    select(table).where(
                        (table.provider == provider) & (table.key == key)
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                expires_at = _aware_utc(row.expires_at)
                if expires_at is not None and expires_at > now():
                    return json.loads(row.payload_json)

    fetched = fetch()
    if hasattr(fetched, "__await__"):
        fetched = await fetched  # type: ignore[assignment]

    payload_json = json.dumps(fetched, default=str)
    payload_hash = _hash_payload(payload_json)
    expires_at = now() + timedelta(seconds=max(ttl_seconds, 0))

    async with db_mod.get_session() as session:
        existing = (
            await session.execute(
                select(table).where(
                    (table.provider == provider) & (table.key == key)
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                table(
                    provider=provider,
                    key=key,
                    payload_json=payload_json,
                    retrieved_at=now(),
                    expires_at=expires_at,
                    payload_hash=payload_hash,
                )
            )
        else:
            existing.payload_json = payload_json
            existing.retrieved_at = now()
            existing.expires_at = expires_at
            existing.payload_hash = payload_hash
        await session.commit()

    return fetched


__all__ = ["CacheKind", "cached_call"]
