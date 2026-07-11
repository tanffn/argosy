"""SEC EDGAR Form 4 adapter (Phase 4).

Source: SEC EDGAR ``/cgi-bin/browse-edgar?action=getcompany&type=4``.

Form 4 is the insider-transactions disclosure officers, directors, and
10%+ holders must file within two business days of a transaction. We
expose two lookup modes:

  - ``get_recent_form4_for_ticker(ticker, days=30)`` — insider activity
    on one company.
  - ``get_recent_form4_for_filer(cik, days=90)`` — one insider's recent
    activity across every issuer they touch.

Implementation notes:

  - SEC requires a polite ``User-Agent: <Org> <email>`` header; sent on
    every request. We share the helper with the 13F adapter via the
    `sec_13f_adapter` module.
  - 24h cache: Form 4 must be filed within 2 business days of the
    transaction; once-daily refresh catches everything.
  - Free, public, no auth.
  - On unreachable site / 5xx / parse failure → ``MissingDataSourceError``.
  - Ticker lookups go through the SEC company-tickers JSON
    (``https://www.sec.gov/files/company_tickers.json``) to map ticker
    → CIK; that map is itself cached for 7 days because issuer ticker
    assignments are stable.

Test injection:

  - ``http_client=fake`` (same shape as the 13F adapter expects).
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.adapters.data.sec_13f_adapter import (
    EDGAR_BASE,
    EDGAR_BROWSE_URL,
    _default_headers,
)
from argosy.logging import get_logger
from argosy.services.adapter_outcomes import track_adapter_call


def _approx_size_bytes(payload: Any) -> int:
    """Cheap size estimate for adapter-outcome tracking."""
    import json as _json

    try:
        return len(_json.dumps(payload, default=str))
    except (TypeError, ValueError):
        return 0

_log = get_logger("argosy.adapters.sec_form4")


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_DAILY_INDEX_BASE = f"{EDGAR_BASE}/Archives/edgar/daily-index"
DEFAULT_TIMEOUT = 15.0
TICKER_TTL_SECONDS = 60 * 60 * 24 * 7   # 7 days; ticker→CIK map is stable
FORM4_TTL_SECONDS = 60 * 60 * 24        # 24h
MAX_GLOBAL_DATE_RANGE_DAYS = 31
MIN_REQUEST_INTERVAL_SECONDS = 0.11

_REQUEST_SLOT_LOCK = threading.Lock()
_NEXT_REQUEST_START_BY_CLOCK: dict[Callable[[], float], float] = {}


# Codes per SEC Form 4 spec — ``transaction_code`` column. Most common:
#   P = open-market or private purchase
#   S = open-market or private sale
#   A = grant/award
#   M = exercise/conversion of derivative
#   F = payment of exercise price or tax via shares delivered
#   G = bona fide gift
#   D = sale to issuer (rare)
TRANSACTION_CODE_MEANING: dict[str, str] = {
    "P": "purchase",
    "S": "sale",
    "A": "grant",
    "M": "option_exercise",
    "F": "tax_withholding",
    "G": "gift",
    "D": "disposition_to_issuer",
    "X": "option_exercise_outofmoney",
    "C": "conversion",
    "W": "acquisition_via_will",
}


class SecForm4Adapter:
    """Insider-transactions feed against SEC EDGAR. Cached. Inject ``http_client``.

    Args:
        http_client: object exposing ``async get(url, *, headers=None,
            params=None) -> Response`` with ``.content``, ``.text``,
            ``.status_code``, and ``.json()``.
        timeout_seconds: per-request timeout.
    """

    PROVIDER = "sec_form4"

    def __init__(
        self,
        *,
        http_client: Any | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = date.today,
        request_interval_seconds: float = MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        if request_interval_seconds < MIN_REQUEST_INTERVAL_SECONDS:
            raise ValueError(
                "request_interval_seconds must be at least "
                f"{MIN_REQUEST_INTERVAL_SECONDS}"
            )
        self._http = http_client
        self._timeout = timeout_seconds
        self._sleep = sleep
        self._clock = clock
        self._today = today
        self._request_interval_seconds = request_interval_seconds

    # ----- public API -------------------------------------------------

    async def get_recent_form4_for_ticker(
        self,
        ticker: str,
        *,
        days: int = 30,
        ttl_seconds: int = FORM4_TTL_SECONDS,
    ) -> list[dict[str, Any]]:
        """Recent Form 4 transactions on ``ticker``.

        Returns rows with ``filer_name``, ``role``, ``ticker``,
        ``transaction_date``, ``transaction_code``, ``shares``,
        ``price_per_share``, ``value_usd``, ``post_transaction_holdings``.

        Raises:
            ValueError: if ``ticker`` is empty.
            MissingDataSourceError: on outage / parse failure / unknown
                ticker (SEC has no CIK for it).
        """
        if not ticker:
            raise ValueError("ticker is required")
        if days <= 0:
            raise ValueError(f"days must be positive; got {days}")
        ticker_norm = ticker.strip().upper()

        with track_adapter_call("sec_form4", target=ticker_norm) as _outcome:
            # Resolve CIK via cached ticker map.
            cik = await self._resolve_cik_for_ticker(ticker_norm, ttl_seconds=ttl_seconds)
            cutoff = (datetime.now(UTC) - timedelta(days=days)).date()

            async def _fetch() -> list[dict[str, Any]]:
                return await self._collect_form4_rows(
                    cik=cik,
                    cutoff=cutoff,
                    only_ticker=ticker_norm,
                )

            payload = await cached_call(
                kind=CacheKind.PRICES,
                provider=self.PROVIDER,
                key=f"by_ticker:{ticker_norm}:days={days}",
                ttl_seconds=ttl_seconds,
                fetch=_fetch,
            )
            _outcome.set_payload_size_bytes(_approx_size_bytes(payload))
            return payload

    async def get_recent_form4_for_filer(
        self,
        cik: str,
        *,
        days: int = 90,
        ttl_seconds: int = FORM4_TTL_SECONDS,
    ) -> list[dict[str, Any]]:
        """Recent Form 4 transactions made by one filer (CIK).

        For corporate-officer filers the CIK is the *insider's* CIK
        (each insider has their own); the issuer is denormalized into
        each row.

        Raises:
            ValueError: on bad input.
            MissingDataSourceError: on outage / parse failure.
        """
        if not cik:
            raise ValueError("cik is required")
        if days <= 0:
            raise ValueError(f"days must be positive; got {days}")
        cik_padded = str(cik).strip().lstrip("0").zfill(10)
        cutoff = (datetime.now(UTC) - timedelta(days=days)).date()

        async def _fetch() -> list[dict[str, Any]]:
            return await self._collect_form4_rows(
                cik=cik_padded,
                cutoff=cutoff,
                only_ticker=None,
            )

        return await cached_call(
            kind=CacheKind.PRICES,
            provider=self.PROVIDER,
            key=f"by_filer:{cik_padded}:days={days}",
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )

    async def get_form4_for_date_range(
        self,
        start_date: date,
        through: date | None = None,
        *,
        ttl_seconds: int = FORM4_TTL_SECONDS,
    ) -> list[dict[str, Any]]:
        """Collect public-issuer Form 4 transactions from daily indexes.

        The range is inclusive and deliberately bounded. Daily indexes are
        fetched and processed sequentially so one caller cannot create
        unbounded SEC request fanout.
        """
        explicit_through = through is not None
        effective_through = through or (self._today() - timedelta(days=1))
        if effective_through < start_date and not explicit_through:
            return []
        if effective_through < start_date:
            raise ValueError("start_date must be on or before end_date")
        day_count = (effective_through - start_date).days + 1
        if day_count > MAX_GLOBAL_DATE_RANGE_DAYS:
            raise ValueError(
                "date range must contain at most "
                f"{MAX_GLOBAL_DATE_RANGE_DAYS} days; got {day_count}"
            )
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")

        business_days = [
            start_date + timedelta(days=offset)
            for offset in range(day_count)
            if (start_date + timedelta(days=offset)).weekday() < 5
        ]
        if not business_days:
            return []

        async def _fetch() -> list[dict[str, Any]]:
            map_ttl = 0 if ttl_seconds == 0 else TICKER_TTL_SECONDS
            _, cik_to_ticker = await self._get_ticker_maps(ttl_seconds=map_ttl)
            collected_filings: list[dict[str, Any]] = []
            for current in business_days:
                filings = await self._fetch_daily_form_index(current)
                for filing in filings:
                    index_cik = str(filing["cik"]).zfill(10)
                    xml_text = await self._fetch_form4_xml(
                        cik=index_cik,
                        accession=str(filing["accession"]),
                    )
                    parsed = _parse_form4_xml(
                        xml_text,
                        accession=str(filing["accession"]),
                    )
                    if not parsed:
                        continue
                    issuer_cik = str(parsed[0].get("issuer_cik") or "")
                    ticker = cik_to_ticker.get(issuer_cik)
                    if ticker is None:
                        continue
                    archive_filename = str(filing["archive_filename"]).lstrip("/")
                    filing_url = f"{EDGAR_BASE}/Archives/{archive_filename}"
                    for row in parsed:
                        document_type = (
                            str(row.get("document_type") or "")
                            or str(filing["document_type"])
                        )
                        row.update(
                            {
                                "filing_url": filing_url,
                                "filed_at": str(filing["filed_at"]),
                                "accession": str(filing["accession"]),
                                "issuer_cik": issuer_cik,
                                "issuer_name": str(
                                    row.get("issuer_name")
                                    or filing["company_name"]
                                ),
                                "ticker": ticker,
                                "document_type": document_type,
                                "is_amendment": bool(
                                    filing["is_amendment"]
                                    or row.get("is_amendment")
                                ),
                                "source_urls": [filing_url],
                            }
                        )
                    collected_filings.append(
                        {
                            "accession": str(filing["accession"]),
                            "filed_at": str(filing["filed_at"]),
                            "filing_url": filing_url,
                            "is_amendment": bool(
                                filing["is_amendment"]
                                or parsed[0].get("is_amendment")
                            ),
                            "issuer_cik": issuer_cik,
                            "filer_cik": str(parsed[0].get("filer_cik") or ""),
                            "filer_name": str(parsed[0].get("filer_name") or ""),
                            "date_of_original_submission": str(
                                parsed[0].get("date_of_original_submission") or ""
                            ),
                            "rows": parsed,
                        }
                    )
            return _select_filing_versions(collected_filings)

        return await cached_call(
            kind=CacheKind.PRICES,
            provider=self.PROVIDER,
            key=(
                f"global:{start_date.isoformat()}:"
                f"{effective_through.isoformat()}"
            ),
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )

    # ----- internals --------------------------------------------------

    async def _get_ticker_maps(
        self, *, ttl_seconds: int = TICKER_TTL_SECONDS
    ) -> tuple[dict[str, str], dict[str, str]]:
        async def _fetch_maps() -> dict[str, dict[str, str]]:
            text = await self._fetch_text(SEC_TICKERS_URL)
            ticker_to_cik, cik_to_ticker = _parse_ticker_maps(text)
            return {
                "ticker_to_cik": ticker_to_cik,
                "cik_to_ticker": cik_to_ticker,
            }

        payload: dict[str, dict[str, str]] = await cached_call(
            kind=CacheKind.PRICES,
            provider=self.PROVIDER,
            key="ticker_maps",
            ttl_seconds=ttl_seconds,
            fetch=_fetch_maps,
        )
        return payload["ticker_to_cik"], payload["cik_to_ticker"]

    async def _resolve_cik_for_ticker(
        self, ticker: str, *, ttl_seconds: int
    ) -> str:
        """Look up CIK for a ticker via the SEC company-tickers JSON.

        Cached for 7 days; ticker→CIK is essentially stable.
        """

        ticker_map, _ = await self._get_ticker_maps(
            ttl_seconds=0 if ttl_seconds == 0 else TICKER_TTL_SECONDS
        )
        cik = ticker_map.get(ticker.upper())
        if not cik:
            raise MissingDataSourceError(
                f"SEC ticker map has no CIK for ticker={ticker!r}; "
                f"verify on https://www.sec.gov/cgi-bin/browse-edgar"
            )
        return str(cik).lstrip("0").zfill(10)

    async def _collect_form4_rows(
        self,
        *,
        cik: str,
        cutoff: date,
        only_ticker: str | None,
    ) -> list[dict[str, Any]]:
        """Walk the browse-edgar atom feed for Form 4 filings ≥ ``cutoff``.

        For each filing: pull its document index, find the Form 4 XML,
        parse it. Each Form 4 typically describes one set of related
        transactions (sometimes >1 row). We flatten to one row per
        ``nonDerivativeTransaction``.
        """
        params = {
            "action": "getcompany",
            "CIK": cik,
            "type": "4",
            "dateb": "",
            "owner": "include",
            "count": "40",
            "output": "atom",
        }
        feed_text = await self._fetch_text(EDGAR_BROWSE_URL, params=params)
        filings = _parse_form4_atom_index(feed_text, cik=cik)
        rows: list[dict[str, Any]] = []
        for filing in filings:
            filed_at = filing.get("filed_at") or ""
            if not _filing_within_window(filed_at, cutoff=cutoff):
                continue
            accession = filing.get("accession_number") or ""
            if not accession:
                continue
            try:
                xml_text = await self._fetch_form4_xml(cik=cik, accession=accession)
            except MissingDataSourceError as exc:
                _log.warning(
                    "sec_form4.filing_skip", accession=accession, reason=str(exc)
                )
                continue
            try:
                parsed = _parse_form4_xml(xml_text, accession=accession)
            except MissingDataSourceError as exc:
                _log.warning(
                    "sec_form4.parse_skip", accession=accession, reason=str(exc)
                )
                continue
            for row in parsed:
                if only_ticker and (row.get("ticker") or "").upper() != only_ticker:
                    continue
                filing_url = str(filing.get("document_url") or "")
                row.update(
                    {
                        "filed_at": filed_at,
                        "filing_url": filing_url,
                        "source_urls": [filing_url] if filing_url else [],
                    }
                )
                rows.append(row)
        return rows

    async def _fetch_daily_form_index(
        self, filing_date: date
    ) -> list[dict[str, Any]]:
        if filing_date.weekday() >= 5:
            return []
        quarter = ((filing_date.month - 1) // 3) + 1
        filename = f"form.{filing_date:%Y%m%d}.idx"
        url = (
            f"{SEC_DAILY_INDEX_BASE}/{filing_date.year}/"
            f"QTR{quarter}/{filename}"
        )
        response = await self._request(url)
        status = getattr(response, "status_code", 0)
        if status != 200:
            raise MissingDataSourceError(
                f"SEC EDGAR returned HTTP {status or '?'} for {url}"
            )
        text = getattr(response, "text", None)
        if text is None:
            raw: bytes = getattr(response, "content", b"")
            text = raw.decode("utf-8", errors="replace")
        return _parse_daily_form_index(text)

    async def _fetch_form4_xml(self, *, cik: str, accession: str) -> str:
        """Resolve a Form 4 filing's XML doc by accession.

        The doc is named ``form4.xml`` or ``primary_doc.xml`` or
        ``<accession>-index.xml``. We pull the ``index.json`` and find
        a doc whose name endswith ``.xml`` and contains 'form4' or is
        the only xml in the directory.
        """
        nodash = accession.replace("-", "")
        cik_clean = cik.lstrip("0") or "0"
        index_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik_clean}/{nodash}/index.json"

        idx = await self._fetch_json(index_url)
        items = (idx.get("directory", {}) or {}).get("item", []) or []
        candidate: str | None = None
        xml_items = [
            it.get("name") for it in items
            if isinstance(it, dict) and (it.get("name") or "").lower().endswith(".xml")
        ]
        # Prefer a name with "form4" in it.
        for name in xml_items:
            if name and "form4" in name.lower():
                candidate = name
                break
        if candidate is None:
            for name in xml_items:
                if name and "primary_doc" in name.lower():
                    candidate = name
                    break
        if candidate is None and xml_items:
            candidate = xml_items[0]
        if candidate is None:
            raise MissingDataSourceError(
                f"SEC EDGAR Form 4 filing has no XML document; accession={accession}"
            )

        doc_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik_clean}/{nodash}/{candidate}"
        return await self._fetch_text(doc_url)

    async def _fetch_text(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> str:
        resp = await self._request(url, params=params)
        if getattr(resp, "status_code", 0) != 200:
            raise MissingDataSourceError(
                f"SEC EDGAR returned HTTP {getattr(resp, 'status_code', '?')} for {url}"
            )
        text = getattr(resp, "text", None)
        if text is None:
            raw: bytes = getattr(resp, "content", b"")
            text = raw.decode("utf-8", errors="replace")
        return text

    async def _request(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> Any:
        try:
            now = self._clock()
            with _REQUEST_SLOT_LOCK:
                reserved_start = max(
                    now,
                    _NEXT_REQUEST_START_BY_CLOCK.get(self._clock, now),
                )
                _NEXT_REQUEST_START_BY_CLOCK[self._clock] = (
                    reserved_start + self._request_interval_seconds
                )
            delay = reserved_start - now
            if delay > 0:
                await self._sleep(delay)
            if self._http is None:
                async with httpx.AsyncClient(
                    timeout=self._timeout, headers=_default_headers()
                ) as client:
                    return await client.get(url, params=params or {})
            return await self._http.get(
                url, headers=_default_headers(), params=params or {}
            )
        except Exception as exc:
            _log.warning("sec_form4.fetch_failed", url=url, reason=str(exc))
            raise MissingDataSourceError(
                f"SEC EDGAR unreachable ({exc!s}); url={url}"
            ) from exc

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        resp = await self._request(url)
        if getattr(resp, "status_code", 0) != 200:
            raise MissingDataSourceError(
                f"SEC EDGAR returned HTTP {getattr(resp, 'status_code', '?')} for {url}"
            )
        try:
            return resp.json() if callable(getattr(resp, "json", None)) else resp.json
        except Exception as exc:
            raise MissingDataSourceError(
                f"SEC EDGAR returned non-JSON for {url}: {exc!s}"
            ) from exc


# ----------------------------------------------------------------------
# Parsing helpers — module-level for direct test exercise
# ----------------------------------------------------------------------


def _parse_ticker_maps(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Parse SEC company tickers into TICKER→CIK and CIK→TICKER maps.

    Expected shape: ``{"0": {"cik_str": ..., "ticker": ..., "title": ...}, ...}``
    """
    import json as _json

    try:
        data = _json.loads(text)
    except _json.JSONDecodeError as exc:
        raise MissingDataSourceError(
            f"SEC company_tickers.json malformed: {exc!s}"
        ) from exc
    ticker_to_cik: dict[str, str] = {}
    cik_to_ticker: dict[str, str] = {}
    entries: Any = []
    if isinstance(data, dict):
        entries = data.values()
    elif isinstance(data, list):
        entries = data
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ticker = (entry.get("ticker") or "").upper().strip()
        cik_value = entry.get("cik_str") or entry.get("cik")
        if ticker and cik_value is not None:
            cik = str(cik_value).lstrip("0").zfill(10)
            ticker_to_cik[ticker] = cik
            cik_to_ticker.setdefault(cik, ticker)
    return ticker_to_cik, cik_to_ticker


def _parse_ticker_map(text: str) -> dict[str, str]:
    """Backward-compatible TICKER → CIK view of company-tickers JSON."""
    return _parse_ticker_maps(text)[0]


def _parse_daily_form_index(text: str) -> list[dict[str, Any]]:
    """Parse one SEC fixed-width daily Form index."""
    lines = text.splitlines()
    if not any("Form Type" in line and "File Name" in line for line in lines):
        raise MissingDataSourceError("SEC EDGAR daily Form index malformed: missing header")
    try:
        separator_index = next(
            index for index, line in enumerate(lines) if line.startswith("-" * 20)
        )
    except StopIteration as exc:
        raise MissingDataSourceError(
            "SEC EDGAR daily Form index malformed: missing separator"
        ) from exc

    rows: list[dict[str, Any]] = []
    for raw_line in lines[separator_index + 1 :]:
        if not raw_line.strip():
            continue
        if len(raw_line) < 99:
            raise MissingDataSourceError(
                "SEC EDGAR daily Form index malformed: invalid data row"
            )
        form_type = raw_line[:12].strip()
        company_name = raw_line[12:74].strip()
        cik_raw = raw_line[74:86].strip()
        filed_at = raw_line[86:98].strip()
        archive_filename = raw_line[98:].strip()
        if (
            not form_type
            or not company_name
            or not cik_raw.isdigit()
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", filed_at)
            or not archive_filename
        ):
            raise MissingDataSourceError(
                "SEC EDGAR daily Form index malformed: invalid columns"
            )
        if form_type not in {"4", "4/A"}:
            continue
        basename = archive_filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}(?:-[A-Za-z0-9]+)?", basename):
            raise MissingDataSourceError(
                "SEC EDGAR daily Form index malformed: invalid Form 4 accession"
            )
        rows.append(
            {
                "cik": cik_raw.lstrip("0").zfill(10),
                "company_name": company_name,
                "filed_at": filed_at,
                "archive_filename": archive_filename,
                "accession": basename,
                "document_type": form_type,
                "is_amendment": form_type == "4/A",
            }
        )
    return rows


def _parse_form4_atom_index(text: str, *, cik: str) -> list[dict[str, Any]]:
    """Parse the browse-edgar atom feed of Form 4 filings."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise MissingDataSourceError(
            f"SEC EDGAR atom feed malformed: {exc!s}"
        ) from exc
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out: list[dict[str, Any]] = []
    for entry in root.findall("a:entry", ns):
        title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
        updated = (entry.findtext("a:updated", default="", namespaces=ns) or "").strip()
        link_el = entry.find("a:link", ns)
        href = (link_el.get("href") if link_el is not None else "") or ""
        accession = ""
        m_dashed = re.search(r"(\d{10})-(\d{2})-(\d{6})", href)
        if m_dashed:
            accession = (
                f"{m_dashed.group(1)}-{m_dashed.group(2)}-{m_dashed.group(3)}"
            )
        else:
            m_nodash = re.search(r"/(\d{10})(\d{2})(\d{6})/", href)
            if m_nodash:
                accession = (
                    f"{m_nodash.group(1)}-{m_nodash.group(2)}-{m_nodash.group(3)}"
                )
        out.append(
            {
                "cik": cik.lstrip("0") or cik,
                "title": title,
                "filed_at": updated,
                "document_url": href,
                "accession_number": accession,
            }
        )
    return out


def _parse_form4_xml(xml_text: str, *, accession: str = "") -> list[dict[str, Any]]:
    """Parse a single Form 4 XML document → list of transaction rows.

    Form 4 XML is namespaced; we use local-name matching to be tolerant
    of namespace prefix drift across filers.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise MissingDataSourceError(
            f"Form 4 XML malformed: {exc!s}"
        ) from exc

    document_type = _ft(root, "documentType")
    is_amendment = document_type.upper() == "4/A"
    period_of_report = _ft(root, "periodOfReport")
    date_of_original_submission = _ft(root, "dateOfOriginalSubmission")
    document_checkbox_10b5 = _is_true(_ft(root, "aff10b5One"))
    remarks = _ft(root, "remarks")
    issuer_cik = _normalize_cik(_ft(root, "issuerCik"))
    issuer_name = _ft(root, "issuerName")
    issuer_ticker = _ft(root, "issuerTradingSymbol")
    # Reporting owner — name + role flags.
    owner_cik = _normalize_cik(_ft(root, "rptOwnerCik"))
    owner_name = _ft(root, "rptOwnerName")
    is_director = (_ft(root, "isDirector") or "").lower() in ("1", "true")
    is_officer = (_ft(root, "isOfficer") or "").lower() in ("1", "true")
    is_ten_percent_owner = (_ft(root, "isTenPercentOwner") or "").lower() in ("1", "true")
    officer_title = _ft(root, "officerTitle")
    role_parts: list[str] = []
    if is_director:
        role_parts.append("director")
    if is_officer:
        role_parts.append("officer" + (f" ({officer_title})" if officer_title else ""))
    if is_ten_percent_owner:
        role_parts.append("10pct_owner")
    role = ", ".join(role_parts) or "unknown"

    footnotes: dict[str, str] = {}
    for footnote in _iter_local(root, "footnote"):
        footnote_id = str(footnote.attrib.get("id") or "").strip()
        if footnote_id:
            footnotes[footnote_id] = " ".join(
                part.strip() for part in footnote.itertext() if part.strip()
            )

    referenced_10b5_footnotes = {
        str(element.attrib.get("id") or "").strip()
        for transaction_name in (
            "nonDerivativeTransaction",
            "derivativeTransaction",
        )
        for transaction in _iter_local(root, transaction_name)
        for element in transaction.iter()
        if _local_name(element.tag) == "footnoteId"
        and _contains_10b5_1(
            footnotes.get(str(element.attrib.get("id") or "").strip(), "")
        )
    }
    document_evidence: list[str] = []
    if document_checkbox_10b5:
        document_evidence.append("document:aff10b5One")
    if remarks and _contains_10b5_1(remarks):
        document_evidence.append(f"remarks:{remarks}")
    for footnote_id in sorted(referenced_10b5_footnotes):
        document_evidence.append(
            f"linked_footnote:{footnote_id}:{footnotes[footnote_id]}"
        )
    document_has_10b5 = bool(document_evidence)

    rows: list[dict[str, Any]] = []
    # Non-derivative transactions (common stock buys/sells).
    for transaction_index, tx in enumerate(
        _iter_local(root, "nonDerivativeTransaction")
    ):
        row = _form4_tx_to_row(
            tx,
            document_type=document_type,
            is_amendment=is_amendment,
            period_of_report=period_of_report,
            date_of_original_submission=date_of_original_submission,
            document_has_10b5=document_has_10b5,
            document_evidence=document_evidence,
            footnotes=footnotes,
            issuer_cik=issuer_cik,
            issuer_ticker=issuer_ticker,
            issuer_name=issuer_name,
            owner_cik=owner_cik,
            owner_name=owner_name,
            role=role,
            accession=accession,
            transaction_index=transaction_index,
        )
        if row is not None:
            rows.append(row)
    # Optional: derivative transactions (options exercises etc.).
    for transaction_index, tx in enumerate(_iter_local(root, "derivativeTransaction")):
        row = _form4_tx_to_row(
            tx,
            document_type=document_type,
            is_amendment=is_amendment,
            period_of_report=period_of_report,
            date_of_original_submission=date_of_original_submission,
            document_has_10b5=document_has_10b5,
            document_evidence=document_evidence,
            footnotes=footnotes,
            issuer_cik=issuer_cik,
            issuer_ticker=issuer_ticker,
            issuer_name=issuer_name,
            owner_cik=owner_cik,
            owner_name=owner_name,
            role=role,
            accession=accession,
            transaction_index=transaction_index,
            derivative=True,
        )
        if row is not None:
            rows.append(row)
    return rows


def _form4_tx_to_row(
    tx: Any,
    *,
    document_type: str,
    is_amendment: bool,
    period_of_report: str,
    date_of_original_submission: str,
    document_has_10b5: bool,
    document_evidence: list[str],
    footnotes: dict[str, str],
    issuer_cik: str,
    issuer_ticker: str,
    issuer_name: str,
    owner_cik: str,
    owner_name: str,
    role: str,
    accession: str,
    transaction_index: int,
    derivative: bool = False,
) -> dict[str, Any] | None:
    code = _ft(tx, "transactionCode")
    tx_date = _ft(tx, "transactionDate")
    security_title = _ft(tx, "securityTitle")
    direct_or_indirect_ownership = _ft(tx, "directOrIndirectOwnership")
    nature_of_ownership = _ft(tx, "natureOfOwnership")
    acquired_disposed_code = _ft(tx, "transactionAcquiredDisposedCode")
    shares_str = _ft(tx, "transactionShares")
    price_str = _ft(tx, "transactionPricePerShare")
    post_str = _ft(tx, "sharesOwnedFollowingTransaction")
    if not code and not tx_date:
        return None
    try:
        shares = float(shares_str.replace(",", "")) if shares_str else None
    except ValueError:
        shares = None
    try:
        price = float(price_str.replace(",", "")) if price_str else None
    except ValueError:
        price = None
    try:
        post_holdings = (
            float(post_str.replace(",", "")) if post_str else None
        )
    except ValueError:
        post_holdings = None
    value_usd: float | None = None
    if shares is not None and price is not None:
        value_usd = shares * price

    tenb5_evidence: list[str] = []
    referenced_footnotes = {
        str(el.attrib.get("id") or "").strip()
        for el in tx.iter()
        if _local_name(el.tag) == "footnoteId"
        and str(el.attrib.get("id") or "").strip()
    }
    for footnote_id in sorted(referenced_footnotes):
        text = footnotes.get(footnote_id, "")
        if text and _contains_10b5_1(text):
            tenb5_evidence.append(f"footnote:{footnote_id}:{text}")

    return {
        "accession": accession,
        "document_type": document_type,
        "is_amendment": is_amendment,
        "period_of_report": period_of_report,
        "date_of_original_submission": date_of_original_submission,
        "filer_cik": owner_cik,
        "filer_name": owner_name,
        "role": role,
        "issuer_cik": issuer_cik,
        "issuer_name": issuer_name,
        "ticker": issuer_ticker,
        "transaction_index": transaction_index,
        "transaction_date": tx_date,
        "transaction_code": code,
        "transaction_kind": TRANSACTION_CODE_MEANING.get(code, "unknown"),
        "security_title": security_title,
        "direct_or_indirect_ownership": direct_or_indirect_ownership,
        "nature_of_ownership": nature_of_ownership,
        "acquired_disposed_code": acquired_disposed_code,
        "shares": shares,
        "price_per_share": price,
        "value_usd": value_usd,
        "post_transaction_holdings": post_holdings,
        "is_derivative": derivative,
        "document_has_10b5_1": document_has_10b5,
        "document_10b5_1_evidence": list(document_evidence),
        "is_10b5_1": True if tenb5_evidence else (None if document_has_10b5 else False),
        "tenb5_1_evidence": tenb5_evidence,
    }


def _contains_10b5_1(text: str) -> bool:
    return re.search(r"\b10b5(?:[\s\W_])*1\b", text, flags=re.IGNORECASE) is not None


def _is_true(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y"}


def _normalize_cik(value: str) -> str:
    normalized = value.strip().lstrip("0")
    return normalized.zfill(10) if normalized else ""


def _normalized_owner_key(filing: dict[str, Any]) -> tuple[str, str]:
    filer_cik = str(filing.get("filer_cik") or "")
    if filer_cik:
        return ("cik", filer_cik.lstrip("0"))
    owner_name = re.sub(
        r"[^a-z0-9]+",
        " ",
        str(filing.get("filer_name") or "").casefold(),
    ).strip()
    return ("name", owner_name)


def _amendment_match_key(
    filing: dict[str, Any],
    *,
    original: bool,
) -> tuple[Any, ...]:
    original_submission_date = (
        str(filing.get("filed_at") or "")[:10]
        if original
        else str(filing.get("date_of_original_submission") or "")[:10]
    )
    return (
        str(filing.get("issuer_cik") or "").lstrip("0"),
        _normalized_owner_key(filing),
        original_submission_date,
    )


def _select_filing_versions(
    filings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    originals = [filing for filing in filings if not filing["is_amendment"]]
    amendments = [filing for filing in filings if filing["is_amendment"]]
    originals_by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for filing in originals:
        originals_by_key.setdefault(
            _amendment_match_key(filing, original=True),
            [],
        ).append(filing)
    amendments_by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for filing in amendments:
        amendments_by_key.setdefault(
            _amendment_match_key(filing, original=False),
            [],
        ).append(filing)

    excluded_original_accessions: set[str] = set()
    selected_filings: list[dict[str, Any]] = []
    for key, versions in amendments_by_key.items():
        candidates = originals_by_key.get(key, [])
        excluded_original_accessions.update(
            str(candidate["accession"]) for candidate in candidates
        )
        latest = max(
            versions,
            key=lambda filing: (
                str(filing.get("filed_at") or ""),
                str(filing.get("accession") or ""),
            ),
        )
        if len(candidates) == 1:
            match_status = "matched"
            cluster_eligible = True
            filing_identity = str(candidates[0]["accession"])
        elif len(candidates) > 1:
            match_status = "ambiguous"
            cluster_eligible = False
            filing_identity = str(latest["accession"])
        else:
            match_status = "unmatched"
            cluster_eligible = False
            filing_identity = str(latest["accession"])
        evidence = sorted(
            str(candidate["accession"]) for candidate in candidates
        )
        source_urls = sorted(
            {
                str(filing["filing_url"])
                for filing in [*candidates, *versions]
                if filing.get("filing_url")
            }
        )
        for row in latest["rows"]:
            row.update(
                {
                    "amendment_match_status": match_status,
                    "amendment_ambiguity_evidence": (
                        evidence if match_status == "ambiguous" else []
                    ),
                    "filing_identity": filing_identity,
                    "cluster_eligible": cluster_eligible,
                    "source_urls": source_urls,
                }
            )
        selected_filings.append(latest)

    selected_filings.extend(
        filing
        for filing in originals
        if str(filing["accession"]) not in excluded_original_accessions
    )
    for filing in selected_filings:
        if filing["is_amendment"]:
            continue
        for row in filing["rows"]:
            row.update(
                {
                    "amendment_match_status": "not_amendment",
                    "amendment_ambiguity_evidence": [],
                    "filing_identity": str(filing["accession"]),
                    "cluster_eligible": True,
                    "source_urls": [filing["filing_url"]],
                }
            )

    selected_filings.sort(
        key=lambda filing: (
            str(filing.get("filed_at") or ""),
            str(filing.get("accession") or ""),
        )
    )
    result: list[dict[str, Any]] = []
    for filing in selected_filings:
        result.extend(filing["rows"])
    return result


def _filing_within_window(filed_at: str, *, cutoff: date) -> bool:
    """Return True iff the ISO-ish ``filed_at`` is on or after ``cutoff``."""
    if not filed_at:
        return False
    # Atom uses RFC3339; Edgar's plain-text dates are 'YYYY-MM-DD'. Be tolerant.
    try:
        if "T" in filed_at:
            dt = datetime.fromisoformat(filed_at.replace("Z", "+00:00"))
            d = dt.date()
        else:
            d = date.fromisoformat(filed_at[:10])
    except ValueError:
        return False
    return d >= cutoff


def _iter_local(node: Any, local: str) -> Any:
    for el in node.iter():
        if _local_name(el.tag) == local:
            yield el


def _ft(node: Any, local: str) -> str:
    """Find local-name ``local`` and return its trimmed text or empty.

    Handles two common Form-4 idioms: a direct ``<value>`` child holding
    the actual scalar, and the unwrapped form.
    """
    for el in node.iter():
        if _local_name(el.tag) != local:
            continue
        # Some Form-4 XMLs wrap scalars in <value>... </value>
        for child in el:
            if _local_name(child.tag) == "value":
                return (child.text or "").strip()
        return (el.text or "").strip()
    return ""


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


__all__ = [
    "FORM4_TTL_SECONDS",
    "MAX_GLOBAL_DATE_RANGE_DAYS",
    "MIN_REQUEST_INTERVAL_SECONDS",
    "SEC_DAILY_INDEX_BASE",
    "SEC_TICKERS_URL",
    "SecForm4Adapter",
    "TICKER_TTL_SECONDS",
    "TRANSACTION_CODE_MEANING",
    "_filing_within_window",
    "_parse_daily_form_index",
    "_parse_form4_atom_index",
    "_parse_form4_xml",
    "_parse_ticker_map",
    "_parse_ticker_maps",
]
