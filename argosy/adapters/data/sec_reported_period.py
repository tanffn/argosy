"""SEC EDGAR reported-period adapter (Stream A Option C).

Primary source of ``most_recent_reported_period`` for US-listed single-name
equities. Uses the free ``data.sec.gov/submissions`` endpoint — no API key.

``reportDate`` on 10-Q / 10-K (and foreign-issuer equivalents) is the fiscal
period end of the filing — authoritative for "has a later quarter been
reported?" Independent of yfinance ``mostRecentQuarter``, which may stamp
``financials_as_of`` (the period the *numbers* cover). Never use one source
for both sides of the vintage independence check.

Reuses the existing SEC User-Agent / rate-limit helpers from the Form 4 /
13F adapters. Sync and async paths share one process-global rate limiter.
Sync submissions responses are durably cached on disk (the async path keeps
using ``cached_call`` / kv_cache).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.adapters.data.sec_13f_adapter import _default_headers
from argosy.adapters.data.sec_errors import (
    SecContactEmailUnsetError,
    SecFailureKind,
    SecHttpStatusError,
    SecProviderError,
    SecTimeoutError,
)
from argosy.adapters.data.sec_form4_adapter import SEC_TICKERS_URL, TICKER_TTL_SECONDS
from argosy.adapters.data.sec_rate_limit import (
    MIN_SEC_REQUEST_INTERVAL_SECONDS,
    validate_sec_request_interval,
    wait_for_sec_request_slot,
    wait_for_sec_request_slot_sync,
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


def _disk_cache_root() -> Path:
    """Durable submissions cache — survives process restart, no async session."""
    home = os.environ.get("ARGOSY_HOME", "").strip()
    base = Path(home) if home else Path.cwd()
    root = base / ".cache" / "sec_submissions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read_disk_cache(key: str, *, ttl_seconds: int) -> dict[str, Any] | None:
    path = _disk_cache_root() / f"{key}.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    retrieved = raw.get("_retrieved_at")
    payload = raw.get("payload")
    if not isinstance(payload, dict) or not isinstance(retrieved, str):
        return None
    try:
        ts = datetime.fromisoformat(retrieved)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age > ttl_seconds:
        return None
    return payload


def _write_disk_cache(key: str, payload: dict[str, Any]) -> None:
    path = _disk_cache_root() / f"{key}.json"
    body = {
        "_retrieved_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    try:
        path.write_text(json.dumps(body), encoding="utf-8")
    except OSError as exc:
        _log.warning(
            "sec_reported_period.disk_cache_write_failed",
            key=key,
            error=str(exc)[:160],
        )


_SYNC_TICKER_MAP: dict[str, str] | None = None
_SYNC_TICKER_MAP_LOADED_AT: float = 0.0
_SYNC_TICKER_MAP_TTL = 60 * 60 * 6


def _raise_for_http_status(status: int, *, context: str) -> None:
    if status == 200:
        return
    raise SecHttpStatusError(status, context)


def _sync_ticker_map(client: httpx.Client) -> dict[str, str]:
    """Process-local + disk-backed cache of SEC ticker→CIK for sync enrich."""
    global _SYNC_TICKER_MAP, _SYNC_TICKER_MAP_LOADED_AT
    now = time.monotonic()
    if (
        _SYNC_TICKER_MAP is not None
        and (now - _SYNC_TICKER_MAP_LOADED_AT) < _SYNC_TICKER_MAP_TTL
    ):
        return _SYNC_TICKER_MAP

    cached = _read_disk_cache("company_tickers", ttl_seconds=_SYNC_TICKER_MAP_TTL)
    if cached is not None:
        out: dict[str, str] = {}
        if isinstance(cached, dict):
            # Either the raw SEC map or our normalized form.
            if cached.get("_normalized"):
                for k, v in cached.items():
                    if k.startswith("_"):
                        continue
                    out[str(k)] = str(v)
            else:
                for row in cached.values():
                    if not isinstance(row, dict):
                        continue
                    t = _normalize_sec_ticker(str(row.get("ticker") or ""))
                    cik = str(row.get("cik_str") or "").lstrip("0").zfill(10)
                    if t and cik and cik != "0000000000":
                        out[t] = cik
        if out:
            _SYNC_TICKER_MAP = out
            _SYNC_TICKER_MAP_LOADED_AT = now
            return out

    wait_for_sec_request_slot_sync(
        interval_seconds=MIN_SEC_REQUEST_INTERVAL_SECONDS,
    )
    try:
        tr = client.get(SEC_TICKERS_URL)
    except httpx.TimeoutException as exc:
        raise SecTimeoutError(f"company_tickers: {exc}") from exc
    _raise_for_http_status(tr.status_code, context="company_tickers")
    tickers = tr.json()
    out = {}
    if isinstance(tickers, dict):
        _write_disk_cache("company_tickers", tickers)
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
        try:
            resp = await client.get(url, headers=_default_headers())
        except httpx.TimeoutException as exc:
            raise SecTimeoutError(str(exc)) from exc
        status = getattr(resp, "status_code", None)
        if status != 200:
            raise SecHttpStatusError(int(status or 0), url)
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise SecProviderError(
                SecFailureKind.MALFORMED,
                f"SEC EDGAR non-JSON for {url}: {exc!s}",
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
            raise SecProviderError(
                SecFailureKind.MALFORMED,
                "SEC company_tickers.json malformed",
            )
        for row in payload.values():
            if not isinstance(row, dict):
                continue
            if _normalize_sec_ticker(str(row.get("ticker") or "")) == want:
                cik = str(row.get("cik_str") or "").lstrip("0").zfill(10)
                if cik and cik != "0000000000":
                    return cik
        raise SecProviderError(
            SecFailureKind.NO_CIK,
            f"SEC ticker map has no CIK for ticker={ticker!r}",
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
                    raise SecProviderError(
                        SecFailureKind.MALFORMED,
                        f"SEC submissions non-object for CIK={cik}",
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
        *,
        ttl_seconds: int = SUBMISSIONS_TTL_SECONDS,
    ) -> date | None:
        """Sync path for plan-synthesis gather / thread-pool contexts.

        Uses blocking httpx — safe outside the event loop. Durably caches
        submissions JSON on disk. Routes every HTTP start through the
        process-global SEC rate limiter (same budget as the async path).
        """
        sym = (ticker or "").strip()
        if not sym:
            return None
        # Fail fast on missing contact email — distinguishable config error.
        try:
            headers = _default_headers()
        except SecContactEmailUnsetError:
            raise
        except ValueError as exc:
            # Legacy callers / older helpers.
            raise SecContactEmailUnsetError(str(exc)) from exc

        with httpx.Client(
            timeout=self._timeout, headers=headers, follow_redirects=True
        ) as client:
            want = _normalize_sec_ticker(sym)
            try:
                cik = _sync_ticker_map(client).get(want)
            except SecProviderError:
                raise
            except httpx.TimeoutException as exc:
                raise SecTimeoutError(str(exc)) from exc
            if not cik:
                raise SecProviderError(
                    SecFailureKind.NO_CIK,
                    f"SEC ticker map has no CIK for ticker={sym!r}",
                )

            cache_key = f"submissions_CIK{cik}"
            cached = _read_disk_cache(cache_key, ttl_seconds=ttl_seconds)
            if cached is not None:
                return latest_reported_period_from_submissions(cached)

            wait_for_sec_request_slot_sync(
                interval_seconds=self._request_interval_seconds,
            )
            url = f"{DATA_SEC_BASE}/submissions/CIK{cik}.json"
            try:
                sr = client.get(url)
            except httpx.TimeoutException as exc:
                raise SecTimeoutError(f"{sym}: {exc}") from exc
            _raise_for_http_status(sr.status_code, context=f"submissions {sym}")
            try:
                payload = sr.json()
            except Exception as exc:  # noqa: BLE001
                raise SecProviderError(
                    SecFailureKind.MALFORMED,
                    f"SEC submissions non-JSON for {sym}: {exc}",
                ) from exc
            if not isinstance(payload, dict):
                raise SecProviderError(
                    SecFailureKind.MALFORMED,
                    f"SEC submissions non-object for {sym}",
                )
            _write_disk_cache(cache_key, payload)
            return latest_reported_period_from_submissions(payload)


__all__ = [
    "SecReportedPeriodAdapter",
    "latest_reported_period_from_submissions",
]
