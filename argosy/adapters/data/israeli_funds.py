"""Dated Israeli mutual-fund NAVs, keyed by security number (not index name).

TheMarker's public fund page embeds the same dated quote it displays in
__NEXT_DATA__. Only ILS mutual funds are supported: foreign-currency funds,
ETFs, missing identities/dates, and old marks fail closed. NAVs are published
in agorot; the returned price is shekels per unit. This is not an order quote.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx
from bs4 import BeautifulSoup

from argosy.services.jobs.transport_failure import classify_transport_failure, retry_data_read


@dataclass(frozen=True)
class FundQuote:
    security_id: str
    name: str
    price_ils: float
    as_of: date
    fetched_at: datetime
    source_url: str
    sha256: str


# Match the total-book hard-stale limit. Marks older than its fresh limit
# remain explicitly soft-stale; fetching them does not make them current.
MAX_NAV_AGE_DAYS = 14
_lock = threading.Lock()
_cache: dict[str, tuple[float, FundQuote | str]] = {}


def parse_fund_quote(html: str, security_id: str, *, today: date | None = None) -> FundQuote:
    if not re.fullmatch(r"5[0-9]{6}", security_id):
        raise ValueError("Expected an Israeli mutual-fund security number")
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        raise ValueError("Fund page has no structured quote")
    state = json.loads(script.get_text())["props"]["apolloState"]
    asset = state[f'ROOT_QUERY.assets({{"ids":"{security_id}"}}).0']
    if str(asset.get("assetNumber")) != security_id or asset.get("type") != "mtf":
        raise ValueError("Fund quote identity/type mismatch")
    currencies = []
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) == 2 and cells[0].get_text(strip=True) == "מטבע עיקרי":
            currencies.append(cells[1].get_text(strip=True))
    if currencies != ["שקל"]:
        raise ValueError("Fund denomination is not explicitly ILS")
    price = float(asset["value"]) / 100.0
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Invalid fund NAV")
    as_of = datetime.fromtimestamp(float(asset["lastTradeTime"]) / 1000, UTC).date()
    now = datetime.now(UTC)
    age = ((today or now.date()) - as_of).days
    if not 0 <= age <= MAX_NAV_AGE_DAYS:
        raise ValueError(f"Fund NAV date ineligible: {as_of} (age={age} days)")
    return FundQuote(security_id, str(asset["name"]), price, as_of, now,
                     f"https://finance.themarker.com/mtf/{security_id}",
                     hashlib.sha256(html.encode("utf-8")).hexdigest())


def get_fund_quote(security_id: str, *, today: date | None = None) -> FundQuote:
    """Bounded retries + single-flight cache; failures are throttled too."""
    if not re.fullmatch(r"5[0-9]{6}", security_id):
        raise ValueError("Expected an Israeli mutual-fund security number")
    with _lock:
        cached = _cache.get(security_id)
        if cached and cached[0] > time.monotonic():
            value = cached[1]
            if isinstance(value, str):
                raise ValueError(value)
            if 0 <= ((today or datetime.now(UTC).date()) - value.as_of).days <= MAX_NAV_AGE_DAYS:
                return value
        try:
            def fetch():
                response = httpx.get(f"https://finance.themarker.com/mtf/{security_id}",
                                     timeout=12, follow_redirects=False)
                response.raise_for_status()
                return response.text
            quote = parse_fund_quote(retry_data_read(fetch), security_id, today=today)
        except Exception as exc:
            delay = max(300, classify_transport_failure(exc).retry_after_seconds or 0)
            _cache[security_id] = (time.monotonic() + delay, str(exc))
            raise
        _cache[security_id] = (time.monotonic() + 3600, quote)
        return quote
