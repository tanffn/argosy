"""SEC EDGAR 13F-HR adapter (Phase 4).

Source: SEC EDGAR full-text search at
``https://efts.sec.gov/LATEST/search-index?q=&forms=13F-HR`` and the
classic ``https://www.sec.gov/cgi-bin/browse-edgar`` per-filer feed.

Form 13F-HR is the quarterly long-positions disclosure all institutional
investment managers with > $100M AUM file with the SEC. We expose:

  - ``list_recent_13f(days=90)`` — recent filings across all filers.
    One row per filing with (cik, fund_name, period_of_report,
    accession_number, filed_at, document_url).
  - ``get_filing_holdings(accession_number)`` — parse the XML
    information table for one filing → (cusip, ticker_or_name, shares,
    value_usd, put_call_flag).
  - ``get_filer_history(cik, quarters=4)`` — track one filer over time.

Implementation notes:

  - Cached in `kv_cache` with a 90-day TTL. 13Fs are quarterly; a
    daily refresh would be wasteful.
  - SEC requires a polite ``User-Agent: <Org> <email>`` header — they
    will rate-limit / block missing or generic UAs. We send
    ``Argosy/<version> <email>``.
  - We deliberately do NOT pin a ``Host`` header. The adapter touches
    two hostnames (``www.sec.gov`` for archives, ``efts.sec.gov`` for
    full-text search). A hard-coded ``Host: www.sec.gov`` made the
    FTS endpoint serve HTTP 404 (CDN routed to an unknown vhost) — see
    SDD §"Project-wide conventions / gotchas". Letting httpx derive
    the header per URL fixes it.
  - Free, public, no auth.
  - Filings are throttled to 10 req/sec by SEC; the adapter never fans
    out so we're naturally below.
  - On HTTP / network failure: the public methods record an outcome
    via ``track_adapter_call`` (status=``http_error`` or
    ``exception``) and return ``[]`` rather than raising. This lets
    one broken adapter call surface in the UI without bringing down a
    synthesis run. Programmer-error invariants (e.g. ``days <= 0``,
    empty accession) still raise ``ValueError``.

Test injection:

  - Pass ``http_client=fake`` (any object exposing async ``get(url, *,
    headers=None) -> Response``-shaped object with ``.content: bytes``,
    ``.text: str``, ``.status_code: int``, ``.json(): dict``).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.logging import get_logger
from argosy.services.adapter_outcomes import track_adapter_call


def _approx_size_bytes(payload: Any) -> int:
    """Cheap size estimate for adapter-outcome tracking."""
    import json as _json

    try:
        return len(_json.dumps(payload, default=str))
    except (TypeError, ValueError):
        return 0

_log = get_logger("argosy.adapters.sec_13f")


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

EDGAR_BASE = "https://www.sec.gov"
EDGAR_FTS_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_BROWSE_URL = f"{EDGAR_BASE}/cgi-bin/browse-edgar"

DEFAULT_TIMEOUT = 15.0
DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 90  # 90 days

ARGOSY_CONTACT_EMAIL = "admin@argosy.local"


def _user_agent() -> str:
    """Polite, SEC-required User-Agent identifying Argosy + contact."""
    from argosy import __version__

    return f"Argosy/{__version__} {ARGOSY_CONTACT_EMAIL}"


def _default_headers() -> dict[str, str]:
    """Polite headers SEC EDGAR accepts.

    Historical note: we used to pin ``Host: www.sec.gov`` here, which is
    correct for ``www.sec.gov`` but wrong for ``efts.sec.gov`` (the
    full-text-search endpoint). The CDN routed the FTS request as an
    unknown vhost and returned HTTP 404, silently breaking the adapter
    (see SDD §"Project-wide conventions / gotchas"). We now omit the
    ``Host`` header so httpx derives it correctly per URL.
    """
    return {
        "User-Agent": _user_agent(),
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json,text/html,*/*",
    }


def _ticker_query(ticker: str | None) -> str:
    """Normalize a ticker (or symbol) into a quoted FTS query string.

    Empty / falsy → empty query (= 'all 13F-HR filings in the window').
    Otherwise: strip + uppercase + wrap in double-quotes so EDGAR treats
    it as an exact phrase. Returning unquoted strings causes EDGAR to
    tokenize, which makes 'AA' match 'AAPL', 'AAL', etc.
    """
    if not ticker:
        return ""
    t = ticker.strip().upper()
    if not t:
        return ""
    return f'"{t}"'


# ----------------------------------------------------------------------
# Adapter
# ----------------------------------------------------------------------


class Sec13FAdapter:
    """13F-HR feed against SEC EDGAR. Cached. Inject ``http_client`` in tests.

    Args:
        http_client: object exposing ``async get(url, *, headers=None,
            params=None) -> Response`` with ``.content``, ``.text``,
            ``.status_code``, ``.json()``. Defaults to a real
            ``httpx.AsyncClient``.
        timeout_seconds: per-request timeout. SEC EDGAR is generally
            fast but the FTS endpoint can be slow under load.
    """

    PROVIDER = "sec_13f"

    def __init__(
        self,
        *,
        http_client: Any | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._http = http_client
        self._timeout = timeout_seconds

    # ----- public API -------------------------------------------------

    async def list_recent_13f(
        self,
        *,
        days: int = 90,
        ticker: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> list[dict[str, Any]]:
        """Return recent 13F-HR filings, optionally narrowed to one ticker.

        Args:
            days: lookback window. Default 90 — one quarter, which is
                the natural cadence of 13Fs.
            ticker: optional symbol filter. When set, the FTS request
                sends ``q="<TICKER>"`` (uppercased, quoted) so EDGAR
                returns only filings mentioning that exact symbol.
                Empty / None → no symbol filter (all 13F-HR filings).
            ttl_seconds: cache lifetime. Default 90 days.

        Returns:
            List of dicts with keys ``cik``, ``fund_name``,
            ``period_of_report``, ``accession_number``, ``filed_at``,
            ``document_url``. Empty list on HTTP error / outage — the
            failure is recorded on the per-synthesis adapter-outcome
            buffer (status=``http_error`` or ``exception``) so the UI
            shows a human-readable reason instead of crashing the run.

        Raises:
            ValueError: if ``days`` is non-positive. (Programmer-error
                guards still raise; only HTTP / network failures are
                swallowed-and-recorded.)
        """
        if days <= 0:
            raise ValueError(f"days must be positive; got {days}")

        q = _ticker_query(ticker)
        # SEC EDGAR FTS expects an actual ISO date range when
        # ``dateRange=custom``. Empty values either 400 or are silently
        # ignored, so we compute the window from ``days``.
        today = datetime.now(UTC).date()
        startdt = (today - timedelta(days=days)).isoformat()
        enddt = today.isoformat()

        with track_adapter_call("sec_13f", target="13F-HR") as _outcome:

            async def _fetch() -> list[dict[str, Any]]:
                params = {
                    "q": q,
                    "forms": "13F-HR",
                    "dateRange": "custom",
                    "startdt": startdt,
                    "enddt": enddt,
                }
                data = await self._fetch_json(EDGAR_FTS_URL, params=params)
                return _parse_fts_hits(data)

            # Include `enddt` AND the (normalized) ticker in the cache
            # key. Without `enddt`, a Monday tick and a Wednesday tick
            # would collide and silently serve the first tick's stale
            # 90-day window. Without the ticker, a per-symbol call
            # would clobber the no-symbol call's result.
            cache_q = q or "all"
            try:
                out: list[dict[str, Any]] = await cached_call(
                    kind=CacheKind.PRICES,
                    provider=self.PROVIDER,
                    key=f"recent:q={cache_q}:days={days}:enddt={enddt}",
                    ttl_seconds=ttl_seconds,
                    fetch=_fetch,
                )
            except MissingDataSourceError as exc:
                # Network / HTTP failure inside ``_fetch``. Surface the
                # outcome so the UI can show "sec_13f: HTTP 404" (or
                # similar) instead of crashing the synthesis run.
                _outcome.record_http_error(
                    status_code=_extract_http_status(exc) or 0,
                    body=str(exc),
                )
                _log.warning(
                    "sec_13f.list_recent_13f.http_error",
                    reason=str(exc).splitlines()[0] if str(exc) else "",
                )
                return []
            _outcome.set_payload_size_bytes(_approx_size_bytes(out))
            return out

    async def get_filing_holdings(
        self,
        accession_number: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> list[dict[str, Any]]:
        """Parse one 13F filing's information table → list of holdings.

        Args:
            accession_number: SEC accession (e.g. ``0001067983-25-000002``)
                or the CIK-bare form (with or without dashes).
            ttl_seconds: cache lifetime.

        Returns:
            List of dicts with ``cusip``, ``name``, ``shares``,
            ``value_usd``, ``put_call``.

        Raises:
            ValueError: if ``accession_number`` is empty.
            MissingDataSourceError: on outage / parse failure / missing
                information table.
        """
        if not accession_number:
            raise ValueError("accession_number is required")
        accession = accession_number.strip()

        with track_adapter_call("sec_13f", target=f"holdings:{accession}") as _outcome:

            async def _fetch() -> list[dict[str, Any]]:
                xml_text = await self._fetch_information_table_xml(accession)
                return _parse_information_table_xml(xml_text)

            try:
                out: list[dict[str, Any]] = await cached_call(
                    kind=CacheKind.PRICES,
                    provider=self.PROVIDER,
                    key=f"holdings:{accession}",
                    ttl_seconds=ttl_seconds,
                    fetch=_fetch,
                )
            except MissingDataSourceError as exc:
                _outcome.record_http_error(
                    status_code=_extract_http_status(exc) or 0,
                    body=str(exc),
                )
                _log.warning(
                    "sec_13f.get_filing_holdings.http_error",
                    accession=accession,
                    reason=str(exc).splitlines()[0] if str(exc) else "",
                )
                return []
            _outcome.set_payload_size_bytes(_approx_size_bytes(out))
            return out

    async def get_filer_history(
        self,
        cik: str,
        *,
        quarters: int = 4,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> list[dict[str, Any]]:
        """Return up to ``quarters`` of 13F-HR filings for one filer.

        Args:
            cik: SEC CIK (zero-padded or bare, e.g. ``0001067983`` for
                Berkshire).
            quarters: max number of filings to return.
            ttl_seconds: cache lifetime.

        Returns:
            List of dicts (most-recent first) with the same shape as
            ``list_recent_13f`` items.
        """
        if not cik:
            raise ValueError("cik is required")
        if quarters <= 0:
            raise ValueError(f"quarters must be positive; got {quarters}")

        cik_padded = str(cik).strip().lstrip("0").zfill(10)

        with track_adapter_call("sec_13f", target=f"history:{cik_padded}") as _outcome:

            async def _fetch() -> list[dict[str, Any]]:
                params = {
                    "action": "getcompany",
                    "CIK": cik_padded,
                    "type": "13F-HR",
                    "dateb": "",
                    "owner": "include",
                    "count": str(max(quarters, 10)),
                    "output": "atom",
                }
                text = await self._fetch_text(EDGAR_BROWSE_URL, params=params)
                return _parse_browse_atom(text, cik=cik_padded)[:quarters]

            try:
                out: list[dict[str, Any]] = await cached_call(
                    kind=CacheKind.PRICES,
                    provider=self.PROVIDER,
                    key=f"history:{cik_padded}:q={quarters}",
                    ttl_seconds=ttl_seconds,
                    fetch=_fetch,
                )
            except MissingDataSourceError as exc:
                _outcome.record_http_error(
                    status_code=_extract_http_status(exc) or 0,
                    body=str(exc),
                )
                _log.warning(
                    "sec_13f.get_filer_history.http_error",
                    cik=cik_padded,
                    reason=str(exc).splitlines()[0] if str(exc) else "",
                )
                return []
            _outcome.set_payload_size_bytes(_approx_size_bytes(out))
            return out

    # ----- internals --------------------------------------------------

    async def _fetch_json(self, url: str, *, params: dict[str, str]) -> dict[str, Any]:
        try:
            if self._http is None:
                async with httpx.AsyncClient(
                    timeout=self._timeout, headers=_default_headers()
                ) as client:
                    resp = await client.get(url, params=params)
            else:
                resp = await self._http.get(url, headers=_default_headers(), params=params)
        except Exception as exc:
            _log.warning("sec_13f.fetch_failed", url=url, reason=str(exc))
            raise MissingDataSourceError(
                f"SEC EDGAR unreachable ({exc!s}); url={url}"
            ) from exc

        status = getattr(resp, "status_code", 0)
        if status != 200:
            raise MissingDataSourceError(
                f"SEC EDGAR returned HTTP {status} for {url}"
            )

        # Some test fakes return a `.json()` callable; real httpx
        # responses also do. Both work uniformly.
        try:
            data = resp.json() if callable(getattr(resp, "json", None)) else resp.json
        except Exception as exc:
            text_preview = (getattr(resp, "text", "") or "")[:200]
            raise MissingDataSourceError(
                f"SEC EDGAR returned non-JSON for {url}: "
                f"{type(exc).__name__}: {exc}; preview={text_preview!r}"
            ) from exc
        return data

    async def _fetch_text(self, url: str, *, params: dict[str, str]) -> str:
        try:
            if self._http is None:
                async with httpx.AsyncClient(
                    timeout=self._timeout, headers=_default_headers()
                ) as client:
                    resp = await client.get(url, params=params)
            else:
                resp = await self._http.get(url, headers=_default_headers(), params=params)
        except Exception as exc:
            _log.warning("sec_13f.fetch_failed", url=url, reason=str(exc))
            raise MissingDataSourceError(
                f"SEC EDGAR unreachable ({exc!s}); url={url}"
            ) from exc

        status = getattr(resp, "status_code", 0)
        if status != 200:
            raise MissingDataSourceError(
                f"SEC EDGAR returned HTTP {status} for {url}"
            )
        text = getattr(resp, "text", None)
        if text is None:
            raw: bytes = getattr(resp, "content", b"")
            text = raw.decode("utf-8", errors="replace")
        return text

    async def _fetch_information_table_xml(self, accession: str) -> str:
        """Resolve a filing's information-table XML by accession number.

        Strategy: hit the filing index JSON at
        ``/Archives/edgar/data/<cik>/<acc-no-dashes>/index.json``,
        find the ``infotable`` document (extension ``.xml`` whose name
        contains ``infotable`` or ``form13fInfoTable``), and pull it.
        """
        # Accession formats: "0001067983-25-000002" (with dashes) or
        # "000106798325000002" (compact). The /Archives/.../<acc> path
        # uses the dashed form; the directory uses the no-dash form.
        dashed = _accession_dashed(accession)
        nodash = dashed.replace("-", "")
        # CIK is the first 10 chars of the no-dash accession.
        cik = nodash[:10].lstrip("0") or "0"

        index_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik}/{nodash}/index.json"
        try:
            if self._http is None:
                async with httpx.AsyncClient(
                    timeout=self._timeout, headers=_default_headers()
                ) as client:
                    idx_resp = await client.get(index_url)
            else:
                idx_resp = await self._http.get(index_url, headers=_default_headers())
        except Exception as exc:
            raise MissingDataSourceError(
                f"SEC EDGAR unreachable ({exc!s}); url={index_url}"
            ) from exc

        if getattr(idx_resp, "status_code", 0) != 200:
            raise MissingDataSourceError(
                f"SEC EDGAR returned HTTP {getattr(idx_resp, 'status_code', '?')} "
                f"for {index_url}"
            )
        try:
            idx = idx_resp.json() if callable(getattr(idx_resp, "json", None)) else idx_resp.json
        except Exception as exc:
            raise MissingDataSourceError(
                f"SEC EDGAR index.json malformed ({exc!s}); accession={accession}"
            ) from exc

        items = (idx.get("directory", {}) or {}).get("item", []) or []
        info_doc: str | None = None
        for it in items:
            name = (it.get("name") or "").lower()
            if name.endswith(".xml") and ("infotable" in name or "form13finfotable" in name):
                info_doc = it.get("name")
                break
        # Fallback: any .xml that isn't the primary form xml
        if info_doc is None:
            for it in items:
                name = (it.get("name") or "").lower()
                if name.endswith(".xml") and "primary_doc" not in name:
                    info_doc = it.get("name")
                    break
        if info_doc is None:
            raise MissingDataSourceError(
                f"SEC EDGAR filing has no infotable XML; accession={accession}"
            )

        doc_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik}/{nodash}/{info_doc}"
        return await self._fetch_text(doc_url, params={})


# ----------------------------------------------------------------------
# Parsing helpers — module-level for direct test exercise
# ----------------------------------------------------------------------


def _parse_fts_hits(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse the SEC EDGAR full-text-search JSON envelope into filing rows.

    Tolerates both the modern ``hits.hits`` Elasticsearch-style envelope
    and a slimmer test-style ``{"results": [...]}`` shape.
    """
    if not isinstance(payload, dict):
        raise MissingDataSourceError(
            f"SEC EDGAR FTS returned non-object envelope: {type(payload).__name__}"
        )

    raw_hits: list[dict[str, Any]]
    if "hits" in payload and isinstance(payload["hits"], dict):
        raw_hits = list(payload["hits"].get("hits", []) or [])
        # Each hit has _source; flatten.
        flattened: list[dict[str, Any]] = []
        for h in raw_hits:
            src = h.get("_source") if isinstance(h, dict) else None
            if isinstance(src, dict):
                flattened.append(src)
            elif isinstance(h, dict):
                flattened.append(h)
        raw_hits = flattened
    elif "results" in payload and isinstance(payload["results"], list):
        raw_hits = list(payload["results"])
    else:
        # Unknown envelope; return empty so callers see an empty list.
        # (Don't raise — empty quarters happen between filing windows.)
        return []

    out: list[dict[str, Any]] = []
    for src in raw_hits:
        if not isinstance(src, dict):
            continue
        cik = str(src.get("ciks") or src.get("cik") or "").strip()
        if isinstance(src.get("ciks"), list) and src["ciks"]:
            cik = str(src["ciks"][0]).strip()
        accession = str(src.get("adsh") or src.get("accession_number") or "").strip()
        fund_name = str(
            src.get("display_names")
            or src.get("entity_name")
            or src.get("fund_name")
            or ""
        )
        if isinstance(src.get("display_names"), list) and src["display_names"]:
            fund_name = str(src["display_names"][0])
        period = str(
            src.get("period_of_report")
            or src.get("periodOfReport")
            or ""
        )
        filed_at = str(
            src.get("file_date")
            or src.get("filed_at")
            or src.get("filedAt")
            or ""
        )
        if not accession:
            continue
        out.append(
            {
                "cik": cik.lstrip("0") or cik,
                "fund_name": fund_name,
                "period_of_report": period,
                "accession_number": accession,
                "filed_at": filed_at,
                "document_url": _filing_url(cik, accession),
            }
        )
    return out


def _parse_browse_atom(text: str, *, cik: str) -> list[dict[str, Any]]:
    """Parse the EDGAR browse-edgar Atom feed into filing rows.

    The Atom feed has one ``<entry>`` per filing with ``<title>``,
    ``<updated>`` (filed date), and a ``<link>`` to the filing index.
    """
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise MissingDataSourceError(
            f"SEC EDGAR browse-edgar atom XML malformed: {exc!s}"
        ) from exc

    ns = {"a": "http://www.w3.org/2005/Atom"}
    out: list[dict[str, Any]] = []
    for entry in root.findall("a:entry", ns):
        title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
        updated = (entry.findtext("a:updated", default="", namespaces=ns) or "").strip()
        link_el = entry.find("a:link", ns)
        href = (link_el.get("href") if link_el is not None else "") or ""
        # Title looks like "13F-HR - Berkshire Hathaway Inc (0001067983) (Filer)"
        # or just "13F-HR - <name>"; we keep it as fund_name for context.
        # Accession comes from the link: either the no-dash directory
        # form (.../000106798325000002/...) or the dashed form
        # (.../0001067983-25-000002-index.htm).
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
        period = ""
        m2 = re.search(r"\(period of report:\s*([0-9-]+)\)", title, re.IGNORECASE)
        if m2:
            period = m2.group(1)
        out.append(
            {
                "cik": cik.lstrip("0") or cik,
                "fund_name": title,
                "period_of_report": period,
                "accession_number": accession,
                "filed_at": updated,
                "document_url": href,
            }
        )
    return out


def _parse_information_table_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse a 13F infotable XML doc → list of holdings.

    The schema (SEC 13F Information Table) wraps each holding in
    ``<infoTable>`` with children ``nameOfIssuer``, ``cusip``, ``value``
    (in thousands of USD before 2023-Q3; in dollars from 2023-Q3 on),
    ``shrsOrPrnAmt/sshPrnamt``, and optionally ``putCall``. We tolerate
    namespaced elements and the unprefixed variant some filers use.
    """
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise MissingDataSourceError(
            f"13F information-table XML malformed: {exc!s}"
        ) from exc

    out: list[dict[str, Any]] = []
    for info_table in _iter_local(root, "infoTable"):
        name = _local_text(info_table, "nameOfIssuer")
        cusip = _local_text(info_table, "cusip")
        value_str = _local_text(info_table, "value")
        put_call = _local_text(info_table, "putCall") or ""
        shares_node = _find_local(info_table, "shrsOrPrnAmt")
        shares_str = ""
        if shares_node is not None:
            shares_str = _local_text(shares_node, "sshPrnamt")
        try:
            value_usd: float | None = float((value_str or "").replace(",", ""))
        except ValueError:
            value_usd = None
        try:
            shares: int | None = int(float((shares_str or "").replace(",", "")))
        except ValueError:
            shares = None

        out.append(
            {
                "cusip": cusip,
                "name": name,
                "shares": shares,
                "value_usd": value_usd,
                "put_call": put_call,
            }
        )
    if not out:
        # An empty infotable is a real edge case (filer with no
        # reportable positions); but if the doc had no <infoTable>
        # nodes at all we treat that as a parse miss.
        if not list(_iter_local(root, "infoTable")):
            raise MissingDataSourceError(
                "13F information-table XML has no <infoTable> elements"
            )
    return out


def _iter_local(node: Any, local: str) -> Any:
    """Iterate descendants whose local name matches, ignoring namespaces."""
    for el in node.iter():
        if _local_name(el.tag) == local:
            yield el


def _find_local(node: Any, local: str) -> Any:
    for el in node.iter():
        if _local_name(el.tag) == local:
            return el
    return None


def _local_text(node: Any, local: str) -> str:
    for el in node.iter():
        if _local_name(el.tag) == local:
            return (el.text or "").strip()
    return ""


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _accession_dashed(accession: str) -> str:
    """Normalize an accession number to the dashed form (NNNNNNNNNN-NN-NNNNNN)."""
    s = accession.strip().replace("-", "")
    if len(s) == 18 and s.isdigit():
        return f"{s[:10]}-{s[10:12]}-{s[12:]}"
    # Already dashed, or partially dashed — return stripped of stray spaces.
    return accession.strip()


def _filing_url(cik: str, accession: str) -> str:
    """Return a stable EDGAR URL for the filing index page."""
    nodash = accession.replace("-", "")
    cik_clean = (cik or "").lstrip("0") or "0"
    return f"{EDGAR_BASE}/Archives/edgar/data/{cik_clean}/{nodash}/"


def _extract_http_status(exc: BaseException) -> int | None:
    """Pull an HTTP status code out of a MissingDataSourceError message.

    The adapter formats its HTTP-error message as
    ``"SEC EDGAR returned HTTP <code> for <url>"``; this helper parses
    that out so the recorded outcome carries the structured status code
    in addition to the human-readable error text. Network exceptions
    (DNS, timeout) won't have a code — returns None in that case so the
    outcome's ``http_status_code`` stays None and the UI can distinguish
    HTTP-level vs. network-level failures.
    """
    m = re.search(r"HTTP\s+(\d{3})\b", str(exc))
    if m:
        try:
            return int(m.group(1))
        except ValueError:  # pragma: no cover - defensive
            return None
    return None


# Quietly load json module — referenced in tests via patches that
# return raw dicts. Imported here so static analysis sees usage.
_ = json


__all__ = [
    "ARGOSY_CONTACT_EMAIL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_TTL_SECONDS",
    "EDGAR_BASE",
    "EDGAR_BROWSE_URL",
    "EDGAR_FTS_URL",
    "Sec13FAdapter",
    "_accession_dashed",
    "_extract_http_status",
    "_parse_browse_atom",
    "_parse_fts_hits",
    "_parse_information_table_xml",
    "_ticker_query",
]
