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

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
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

_log = get_logger("argosy.adapters.sec_form4")


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
DEFAULT_TIMEOUT = 15.0
TICKER_TTL_SECONDS = 60 * 60 * 24 * 7   # 7 days; ticker→CIK map is stable
FORM4_TTL_SECONDS = 60 * 60 * 24        # 24h


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
    ) -> None:
        self._http = http_client
        self._timeout = timeout_seconds

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

        # Resolve CIK via cached ticker map.
        cik = await self._resolve_cik_for_ticker(ticker_norm, ttl_seconds=ttl_seconds)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()

        async def _fetch() -> list[dict[str, Any]]:
            return await self._collect_form4_rows(
                cik=cik,
                cutoff=cutoff,
                only_ticker=ticker_norm,
            )

        return await cached_call(
            kind=CacheKind.PRICES,
            provider=self.PROVIDER,
            key=f"by_ticker:{ticker_norm}:days={days}",
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )

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
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()

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

    # ----- internals --------------------------------------------------

    async def _resolve_cik_for_ticker(
        self, ticker: str, *, ttl_seconds: int
    ) -> str:
        """Look up CIK for a ticker via the SEC company-tickers JSON.

        Cached for 7 days; ticker→CIK is essentially stable.
        """

        async def _fetch_map() -> dict[str, str]:
            text = await self._fetch_text(SEC_TICKERS_URL)
            return _parse_ticker_map(text)

        ticker_map: dict[str, str] = await cached_call(
            kind=CacheKind.PRICES,
            provider=self.PROVIDER,
            key="ticker_map",
            ttl_seconds=TICKER_TTL_SECONDS,
            fetch=_fetch_map,
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
                rows.append(row)
        return rows

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
        try:
            if self._http is None:
                async with httpx.AsyncClient(
                    timeout=self._timeout, headers=_default_headers()
                ) as client:
                    resp = await client.get(url, params=params or {})
            else:
                resp = await self._http.get(
                    url, headers=_default_headers(), params=params or {}
                )
        except Exception as exc:
            _log.warning("sec_form4.fetch_failed", url=url, reason=str(exc))
            raise MissingDataSourceError(
                f"SEC EDGAR unreachable ({exc!s}); url={url}"
            ) from exc

        if getattr(resp, "status_code", 0) != 200:
            raise MissingDataSourceError(
                f"SEC EDGAR returned HTTP {getattr(resp, 'status_code', '?')} for {url}"
            )
        text = getattr(resp, "text", None)
        if text is None:
            raw: bytes = getattr(resp, "content", b"")
            text = raw.decode("utf-8", errors="replace")
        return text

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        try:
            if self._http is None:
                async with httpx.AsyncClient(
                    timeout=self._timeout, headers=_default_headers()
                ) as client:
                    resp = await client.get(url)
            else:
                resp = await self._http.get(url, headers=_default_headers())
        except Exception as exc:
            raise MissingDataSourceError(
                f"SEC EDGAR unreachable ({exc!s}); url={url}"
            ) from exc

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


def _parse_ticker_map(text: str) -> dict[str, str]:
    """Parse SEC's company-tickers JSON into a TICKER → CIK map.

    Expected shape: ``{"0": {"cik_str": ..., "ticker": ..., "title": ...}, ...}``
    """
    import json as _json

    try:
        data = _json.loads(text)
    except _json.JSONDecodeError as exc:
        raise MissingDataSourceError(
            f"SEC company_tickers.json malformed: {exc!s}"
        ) from exc
    out: dict[str, str] = {}
    if isinstance(data, dict):
        # Could be the index-keyed dict (most common) or a flat list.
        iterator: Any
        if all(k.isdigit() for k in (data.keys() if data else [])):
            iterator = data.values()
        else:
            iterator = data.values() if data else []
        for entry in iterator:
            if not isinstance(entry, dict):
                continue
            t = (entry.get("ticker") or "").upper().strip()
            cik = entry.get("cik_str") or entry.get("cik")
            if t and cik is not None:
                out[t] = str(cik).lstrip("0").zfill(10)
    elif isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            t = (entry.get("ticker") or "").upper().strip()
            cik = entry.get("cik_str") or entry.get("cik")
            if t and cik is not None:
                out[t] = str(cik).lstrip("0").zfill(10)
    return out


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

    issuer_name = _ft(root, "issuerName")
    issuer_ticker = _ft(root, "issuerTradingSymbol")
    # Reporting owner — name + role flags.
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

    rows: list[dict[str, Any]] = []
    # Non-derivative transactions (common stock buys/sells).
    for tx in _iter_local(root, "nonDerivativeTransaction"):
        row = _form4_tx_to_row(
            tx,
            issuer_ticker=issuer_ticker,
            issuer_name=issuer_name,
            owner_name=owner_name,
            role=role,
            accession=accession,
        )
        if row is not None:
            rows.append(row)
    # Optional: derivative transactions (options exercises etc.).
    for tx in _iter_local(root, "derivativeTransaction"):
        row = _form4_tx_to_row(
            tx,
            issuer_ticker=issuer_ticker,
            issuer_name=issuer_name,
            owner_name=owner_name,
            role=role,
            accession=accession,
            derivative=True,
        )
        if row is not None:
            rows.append(row)
    return rows


def _form4_tx_to_row(
    tx: Any,
    *,
    issuer_ticker: str,
    issuer_name: str,
    owner_name: str,
    role: str,
    accession: str,
    derivative: bool = False,
) -> dict[str, Any] | None:
    code = _ft(tx, "transactionCode")
    tx_date = _ft(tx, "transactionDate")
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

    return {
        "accession": accession,
        "filer_name": owner_name,
        "role": role,
        "issuer_name": issuer_name,
        "ticker": issuer_ticker,
        "transaction_date": tx_date,
        "transaction_code": code,
        "transaction_kind": TRANSACTION_CODE_MEANING.get(code, "unknown"),
        "shares": shares,
        "price_per_share": price,
        "value_usd": value_usd,
        "post_transaction_holdings": post_holdings,
        "is_derivative": derivative,
    }


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
    "SEC_TICKERS_URL",
    "SecForm4Adapter",
    "TICKER_TTL_SECONDS",
    "TRANSACTION_CODE_MEANING",
    "_filing_within_window",
    "_parse_form4_atom_index",
    "_parse_form4_xml",
    "_parse_ticker_map",
]
