"""Fresh quote plumbing shared by both settled-verdict trigger loops."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.adapters.data.yfinance_adapter import YFinanceAdapter
from argosy.logging import get_logger
from argosy.state.models import KvCacheEntry

log = get_logger(__name__)


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _price(payload_json: str) -> float | None:
    try:
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            return None
        value = payload.get("price") or payload.get("last") or payload.get("close")
        price = float(value) if value is not None else None
        return price if price is not None and math.isfinite(price) and price > 0 else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


async def _fetch_missing(
    symbols: list[str], adapter: Any, *, concurrency: int = 4,
) -> dict[str, float]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one(symbol: str) -> tuple[str, float | None]:
        try:
            async with semaphore:
                quote = await adapter.get_quote(symbol, ttl_seconds=300)
            return symbol, float(quote.price) if quote.price is not None else None
        except Exception as exc:  # noqa: BLE001 - each symbol is independent
            log.warning(
                "verdict_trigger.live_quote_failed",
                symbol=symbol,
                error=str(exc)[:160],
            )
            return symbol, None

    results = await asyncio.gather(*(_one(symbol) for symbol in symbols))
    return {symbol: price for symbol, price in results if price is not None}


def fetch_trigger_quotes(
    session: Session,
    subjects: list[str],
    *,
    now: datetime | None = None,
    live: bool = True,
    adapter: Any | None = None,
) -> dict[str, float]:
    """Return fresh quotes, fetching Yahoo only for cache misses/expiry.

    ``kv_cache`` has the composite key ``(provider, key)`` and stores JSON in
    ``payload_json``. Both trigger loops previously queried fictional ``value``
    and ``id`` columns, making every price trigger unevaluable on the real DB.
    Expired rows are never used to trip a money decision.
    """
    normalized = list(
        dict.fromkeys(
            str(subject).strip().upper()
            for subject in subjects
            if str(subject).strip()
        )
    )
    if not normalized:
        return {}
    as_of = _aware_utc(now or datetime.now(UTC))
    keys = {f"quote:{subject}": subject for subject in normalized}
    rows = session.execute(
        select(KvCacheEntry)
        .where(
            KvCacheEntry.provider == YFinanceAdapter.PROVIDER,
            KvCacheEntry.key.in_(keys),
        )
        .order_by(KvCacheEntry.retrieved_at.desc())
    ).scalars().all()
    quotes: dict[str, float] = {}
    for row in rows:
        symbol = keys.get(row.key)
        if symbol is None or symbol in quotes:
            continue
        if _aware_utc(row.expires_at) <= as_of:
            continue
        price = _price(row.payload_json)
        if price is not None:
            quotes[symbol] = price

    missing = [symbol for symbol in normalized if symbol not in quotes]
    if live and missing:
        quotes.update(
            asyncio.run(_fetch_missing(missing, adapter or YFinanceAdapter()))
        )
    return quotes


__all__ = ["fetch_trigger_quotes"]
