"""SEC EDGAR reported-period adapter (Stream A Option C).

Primary source of ``most_recent_reported_period`` for US-listed single-name
equities. Uses the free ``data.sec.gov/submissions`` endpoint — no API key.

``reportDate`` on 10-Q / 10-K (and foreign-issuer equivalents) is the fiscal
period end of the filing — authoritative for "has a later quarter been
reported?" Independent of yfinance ``mostRecentQuarter``, which may stamp
``financials_as_of`` (the period the *numbers* cover). Never use one source
for both sides of the vintage independence check.

Reuses the existing SEC User-Agent / rate-limit helpers from the Form 4 /
13F adapters. Does **not** invent periods from filing dates.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import httpx

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.adapters.data.sec_13f_adapter import _default_headers
from argosy.adapters.data.sec_form4_adapter import SEC_TICKERS_URL, TICKER_TTL_SECONDS
from argosy.adapters.data.sec_rate_limit import (
    MIN_SEC_REQUEST_INTERVAL_SECONDS,
    validate_sec_request_interval,
    wait_for_sec_request_slot,
)
from argosy.logging import get_logger
from argosy.services.adapter_outcomes import track_adapter_call

_log = get_logger("argosy.adapters.sec_reported_period")

DATA_SEC_BASE = "https://data.sec.gov"
DEFAULT_TIMEOUT = 20.0
SUBMISSIONS_TTL_SECONDS = 60 * 60 * 12  # 12h — filings land a few times/quarter

# Forms whose reportDate is a fiscal period end we can trust.
_PERIOD_FORMS: frozenset[str] = frozenset(
    {
        "10-Q",
        "10-Q/A",
        "10-K",
        "10-K/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
    }
)


def _normalize_sec_ticker(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if not t:
        return t
    # SEC company_tickers.json uses BRK-B; the book uses BRK/B.
    return t.replace("/", "-").replace(".", "-")


def _parse_iso_date(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    if len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def latest_reported_period_from_submissions(payload: dict[str, Any]) -> date | None:
    """Extract the latest fiscal period-end from a submissions JSON body.

    Returns None when no usable period form is present — never invents.
    """
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    report_dates = recent.get("reportDate") or []
    if not isinstance(forms, list) or not isinstance(report_dates, list):
        return None
    periods: list[date] = []
    for i, form in enumerate(forms):
        if str(form) not in _PERIOD_FORMS:
            continue
        if i >= len(report_dates):
            continue
        d = _parse_iso_date(report_dates[i])
        if d is not None:
            periods.append(d)
    return max(periods) if periods else None


_SYNC_TICKER_MAP: dict[str, str] | None = None
_SYNC_TICKER_MAP_LOADED_AT: float = 0.0
_SYNC_TICKER_MAP_TTL = 60 * 60 * 6


def _sync_ticker_map(client: httpx.Client) -> dict[str, str]:
    """Process-local cache of SEC ticker→CIK for the sync enrich path."""
    global _SYNC_TICKER_MAP, _SYNC_TICKER_MAP_LOADED_AT
    now = time.monotonic()
    if (
        _SYNC_TICKER_MAP is not None
        and (now - _SYNC_TICKER_MAP_LOADED_AT) < _SYNC_TICKER_MAP_TTL
    ):
        return _SYNC_TICKER_MAP
    time.sleep(MIN_SEC_REQUEST_INTERVAL_SECONDS)
    tr = client.get(SEC_TICKERS_URL)
    if tr.status_code != 200:
        raise MissingDataSourceError(
            f"SEC company_tickers HTTP {tr.status_code}"
        )
    tickers = tr.json()
    out: dict[str, str] = {}
    if isinstance(tickers, dict):
        for row in tickers.values():
            if not isinstance(row, dict):
                continue
            t = _normalize_sec_ticker(str(row.get("ticker") or ""))
            cik = str(row.get("cik_str") or "").lstrip("0").zfill(10)
            if t and cik and cik != "0000000000":
                out[t] = cik
    _SYNC_TICKER_MAP = out
    _SYNC_TICKER_MAP_LOADED_AT = now
    return out


class SecReportedPeriodAdapter:
    """Reported fiscal period for one US-listed issuer via EDGAR submissions."""

    PROVIDER = "sec_reported_period"

    def __init__(
        self,
        *,
        http_client: Any | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        request_interval_seconds: float = MIN_SEC_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        validate_sec_request_interval(request_interval_seconds)
        self._http = http_client
        self._timeout = timeout_seconds
        self._sleep = sleep
        self._clock = clock
        self._request_interval_seconds = request_interval_seconds

    def _client(self) -> httpx.AsyncClient | Any:
        if self._http is not None:
            return self._http
        self._http = httpx.AsyncClient(
            timeout=self._timeout,
            headers=_default_headers(),
            follow_redirects=True,
        )
        return self._http

    async def _get_json(self, url: str) -> Any:
        await wait_for_sec_request_slot(
            clock=self._clock,
            sleep=self._sleep,
            interval_seconds=self._request_interval_seconds,
        )
        client = self._client()
        resp = await client.get(url, headers=_default_headers())
        status = getattr(resp, "status_code", None)
        if status != 200:
            raise MissingDataSourceError(
                f"SEC EDGAR HTTP {status} for {url}"
            )
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise MissingDataSourceError(
                f"SEC EDGAR non-JSON for {url}: {exc!s}"
            ) from exc

    async def _ticker_to_cik(self, ticker: str) -> str:
        key = "company_tickers"

        async def _fetch() -> Any:
            return await self._get_json(SEC_TICKERS_URL)

        with track_adapter_call(self.PROVIDER, target="ticker_map") as outcome:
            payload = await cached_call(
                kind=CacheKind.NEWS,
                provider=self.PROVIDER,
                key=key,
                ttl_seconds=TICKER_TTL_SECONDS,
                fetch=_fetch,
            )
            outcome.set_payload_size_bytes(len(str(payload)))
        want = _normalize_sec_ticker(ticker)
        if not isinstance(payload, dict):
            raise MissingDataSourceError("SEC company_tickers.json malformed")
        for row in payload.values():
            if not isinstance(row, dict):
                continue
            if _normalize_sec_ticker(str(row.get("ticker") or "")) == want:
                cik = str(row.get("cik_str") or "").lstrip("0").zfill(10)
                if cik and cik != "0000000000":
                    return cik
        raise MissingDataSourceError(
            f"SEC ticker map has no CIK for ticker={ticker!r}"
        )

    async def get_most_recent_reported_period(
        self,
        ticker: str,
        *,
        ttl_seconds: int = SUBMISSIONS_TTL_SECONDS,
    ) -> date | None:
        """Async: latest fiscal period-end from EDGAR submissions."""
        sym = (ticker or "").strip()
        if not sym:
            return None
        with track_adapter_call(self.PROVIDER, target=sym) as outcome:
            cik = await self._ticker_to_cik(sym)
            key = f"submissions:{cik}"
            url = f"{DATA_SEC_BASE}/submissions/CIK{cik}.json"

            async def _fetch() -> dict[str, Any]:
                raw = await self._get_json(url)
                if not isinstance(raw, dict):
                    raise MissingDataSourceError(
                        f"SEC submissions non-object for CIK={cik}"
                    )
                return raw

            payload = await cached_call(
                kind=CacheKind.NEWS,
                provider=self.PROVIDER,
                key=key,
                ttl_seconds=ttl_seconds,
                fetch=_fetch,
            )
            period = latest_reported_period_from_submissions(payload)
            outcome.set_payload_size_bytes(len(str(payload)) if payload else 0)
            return period

    def get_most_recent_reported_period_sync(
        self,
        ticker: str,
    ) -> date | None:
        """Sync path for plan-synthesis gather / thread-pool contexts.

        Uses blocking httpx — safe outside the event loop. Does **not**
        touch the async cache table (avoids MissingGreenlet).
        """
        sym = (ticker or "").strip()
        if not sym:
            return None
        headers = _default_headers()
        with httpx.Client(
            timeout=self._timeout, headers=headers, follow_redirects=True
        ) as client:
            want = _normalize_sec_ticker(sym)
            cik = _sync_ticker_map(client).get(want)
            if not cik:
                raise MissingDataSourceError(
                    f"SEC ticker map has no CIK for ticker={sym!r}"
                )
            time.sleep(self._request_interval_seconds)
            url = f"{DATA_SEC_BASE}/submissions/CIK{cik}.json"
            sr = client.get(url)
            if sr.status_code != 200:
                raise MissingDataSourceError(
                    f"SEC submissions HTTP {sr.status_code} for {sym}"
                )
            return latest_reported_period_from_submissions(sr.json())


__all__ = [
    "SecReportedPeriodAdapter",
    "latest_reported_period_from_submissions",
]
