"""SEC 13F adapter tests — pure parsers + DI'd HTTP.

We never hit the network; every test patches the adapter's `http_client`.
"""

from __future__ import annotations

import json
from datetime import UTC
from typing import Any

import pytest

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.sec_13f_adapter import (
    Sec13FAdapter,
    _accession_dashed,
    _extract_http_status,
    _parse_browse_atom,
    _parse_fts_hits,
    _parse_information_table_xml,
    _ticker_query,
)
from argosy.services.adapter_outcomes import (
    collect_outcomes,
    reset_outcomes,
)

# ----------------------------------------------------------------------
# Fixture payloads
# ----------------------------------------------------------------------


_FTS_PAYLOAD = {
    "hits": {
        "hits": [
            {
                "_source": {
                    "ciks": ["0001067983"],
                    "display_names": ["BERKSHIRE HATHAWAY INC"],
                    "adsh": "0001067983-25-000002",
                    "period_of_report": "2025-12-31",
                    "file_date": "2026-02-14",
                }
            },
            {
                "_source": {
                    "ciks": ["0001350694"],
                    "display_names": ["BRIDGEWATER ASSOCIATES, LP"],
                    "adsh": "0001350694-25-000010",
                    "period_of_report": "2025-12-31",
                    "file_date": "2026-02-14",
                }
            },
        ]
    }
}


_BROWSE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>13F-HR - BERKSHIRE HATHAWAY INC (period of report: 2025-12-31)</title>
    <updated>2026-02-14T16:30:00-05:00</updated>
    <link href="https://www.sec.gov/Archives/edgar/data/1067983/0001067983-25-000002-index.htm"/>
  </entry>
  <entry>
    <title>13F-HR - BERKSHIRE HATHAWAY INC (period of report: 2025-09-30)</title>
    <updated>2025-11-14T16:30:00-05:00</updated>
    <link href="https://www.sec.gov/Archives/edgar/data/1067983/0001067983-25-000001-index.htm"/>
  </entry>
</feed>
"""


_INFOTABLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>183000000000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>905560000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>DFND</investmentDiscretion>
  </infoTable>
  <infoTable>
    <nameOfIssuer>BANK OF AMERICA CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>060505104</cusip>
    <value>32500000000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>1032852000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>DFND</investmentDiscretion>
    <putCall>Put</putCall>
  </infoTable>
</informationTable>
"""


_INDEX_JSON = {
    "directory": {
        "item": [
            {"name": "0001067983-25-000002-index.htm"},
            {"name": "primary_doc.xml"},
            {"name": "form13fInfoTable.xml"},
        ]
    }
}


# ----------------------------------------------------------------------
# Fake HTTP shim
# ----------------------------------------------------------------------


class _FakeResp:
    def __init__(
        self,
        *,
        status: int = 200,
        text: str | None = None,
        content: bytes | None = None,
        json_payload: Any | None = None,
    ) -> None:
        self.status_code = status
        self.text = text or (
            json.dumps(json_payload) if json_payload is not None else ""
        )
        self.content = content if content is not None else self.text.encode("utf-8")
        self._json = json_payload

    def json(self) -> Any:
        if self._json is not None:
            return self._json
        return json.loads(self.text)


class _RoutedHttp:
    """Routes specific URL substrings to canned responses; defaults to 404.

    Captures both the URL and the outgoing ``params`` dict on each call so
    tests can assert on the wire-level date window (``startdt`` /
    ``enddt``) the adapter sends — caching by computed date is part of
    the contract and we want a regression guard at the boundary.
    """

    def __init__(self, routes: dict[str, _FakeResp]) -> None:
        self._routes = routes
        self.calls: list[str] = []
        self.calls_with_params: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        self.calls.append(url)
        params = dict(kwargs.get("params") or {})
        self.calls_with_params.append((url, params))
        for needle, resp in self._routes.items():
            if needle in url:
                return resp
        return _FakeResp(status=404, text="not-routed")


class _FailingHttp:
    async def get(self, url: str, **_kwargs: Any) -> _FakeResp:
        raise OSError(f"DNS failure (simulated) {url}")


# ----------------------------------------------------------------------
# Pure-parsing tests (no DB)
# ----------------------------------------------------------------------


def test_parse_fts_hits_basic() -> None:
    rows = _parse_fts_hits(_FTS_PAYLOAD)
    assert len(rows) == 2
    assert rows[0]["accession_number"] == "0001067983-25-000002"
    assert rows[0]["fund_name"] == "BERKSHIRE HATHAWAY INC"
    assert rows[0]["period_of_report"] == "2025-12-31"
    assert rows[0]["filed_at"] == "2026-02-14"
    assert "1067983" in rows[0]["document_url"]


def test_parse_fts_hits_results_envelope() -> None:
    payload = {
        "results": [
            {
                "cik": "1067983",
                "fund_name": "Berkshire",
                "accession_number": "0001067983-25-000002",
                "period_of_report": "2025-12-31",
                "filed_at": "2026-02-14",
            }
        ]
    }
    rows = _parse_fts_hits(payload)
    assert len(rows) == 1
    assert rows[0]["fund_name"] == "Berkshire"


def test_parse_fts_hits_unknown_envelope_returns_empty() -> None:
    assert _parse_fts_hits({"unrelated": []}) == []


def test_parse_browse_atom() -> None:
    rows = _parse_browse_atom(_BROWSE_ATOM, cik="0001067983")
    assert len(rows) == 2
    assert rows[0]["accession_number"] == "0001067983-25-000002"
    assert rows[0]["period_of_report"] == "2025-12-31"
    assert rows[0]["filed_at"].startswith("2026-02-14")
    assert "1067983" in rows[0]["document_url"]


def test_parse_information_table_xml() -> None:
    holdings = _parse_information_table_xml(_INFOTABLE_XML)
    assert len(holdings) == 2
    apple = next(h for h in holdings if h["cusip"] == "037833100")
    assert apple["name"] == "APPLE INC"
    assert apple["shares"] == 905560000
    assert apple["value_usd"] == 183_000_000_000.0
    assert apple["put_call"] == ""
    bofa = next(h for h in holdings if h["cusip"] == "060505104")
    assert bofa["put_call"] == "Put"


def test_parse_information_table_xml_malformed_raises() -> None:
    with pytest.raises(MissingDataSourceError):
        _parse_information_table_xml("<not-xml>")


def test_parse_information_table_xml_no_rows_raises() -> None:
    empty = """<?xml version="1.0" encoding="UTF-8"?>
    <informationTable xmlns="http://www.sec.gov/x"><x/></informationTable>"""
    with pytest.raises(MissingDataSourceError):
        _parse_information_table_xml(empty)


def test_accession_dashed() -> None:
    assert _accession_dashed("000106798325000002") == "0001067983-25-000002"
    assert _accession_dashed("0001067983-25-000002") == "0001067983-25-000002"


# ----------------------------------------------------------------------
# Adapter tests (cache → in-memory SQLite via `engine` fixture)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_recent_13f_caches(engine: None) -> None:
    fake = _RoutedHttp({"efts.sec.gov": _FakeResp(json_payload=_FTS_PAYLOAD)})
    adapter = Sec13FAdapter(http_client=fake)

    rows = await adapter.list_recent_13f(days=90)
    assert len(rows) == 2
    rows2 = await adapter.list_recent_13f(days=90)
    assert rows == rows2
    # Second call served from cache; HTTP not re-hit.
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_list_recent_13f_sends_date_window(engine: None) -> None:
    """The adapter must emit ``startdt = today - days`` and ``enddt = today``
    as ISO dates on the FTS request. Empty / missing values either 400
    or are silently ignored by SEC EDGAR, so this is contract-level."""
    from datetime import datetime, timedelta

    fake = _RoutedHttp({"efts.sec.gov": _FakeResp(json_payload=_FTS_PAYLOAD)})
    adapter = Sec13FAdapter(http_client=fake)
    await adapter.list_recent_13f(days=90)

    assert fake.calls_with_params, "expected at least one HTTP call"
    _url, params = fake.calls_with_params[0]
    today = datetime.now(UTC).date()
    expected_start = (today - timedelta(days=90)).isoformat()
    expected_end = today.isoformat()
    assert params.get("startdt") == expected_start, params
    assert params.get("enddt") == expected_end, params
    assert params.get("forms") == "13F-HR", params
    assert params.get("dateRange") == "custom", params


@pytest.mark.asyncio
async def test_list_recent_13f_invalid_days(engine: None) -> None:
    adapter = Sec13FAdapter(http_client=_FailingHttp())
    with pytest.raises(ValueError):
        await adapter.list_recent_13f(days=0)


@pytest.mark.asyncio
async def test_list_recent_13f_outage_records_outcome_and_returns_empty(
    engine: None,
) -> None:
    """Network failure inside the FTS fetch must NOT crash the synthesis.

    Contract per T3.1: record an outcome (status=``exception`` because
    the inner OSError doesn't carry an HTTP code) and return ``[]``.
    Prior behavior raised ``MissingDataSourceError``, which blew up the
    whole run when one adapter was flaky — the user explicitly said
    "we don't need to crash, but we need to surface issues."
    """
    reset_outcomes()
    adapter = Sec13FAdapter(http_client=_FailingHttp())
    rows = await adapter.list_recent_13f(days=30)
    assert rows == []
    outcomes = [o for o in collect_outcomes() if o.adapter_name == "sec_13f"]
    assert len(outcomes) == 1, outcomes
    # ``_FailingHttp`` raises OSError with no embedded HTTP code, so the
    # adapter wraps it as a MissingDataSourceError (unreachable site).
    # The error_text carries the human-readable reason.
    assert outcomes[0].status == "http_error"
    assert outcomes[0].error_text is not None
    assert "unreachable" in outcomes[0].error_text.lower() or \
        "DNS failure" in outcomes[0].error_text
    # No HTTP status code (network-level failure, not a 4xx/5xx).
    assert outcomes[0].http_status_code in (0, None)


@pytest.mark.asyncio
async def test_list_recent_13f_http_404_records_outcome_and_returns_empty(
    engine: None,
) -> None:
    """HTTP 404 from EDGAR FTS → empty list + outcome with code 404.

    This is the failure mode T3.1 was opened to fix: previously the
    adapter raised MissingDataSourceError on 404 and the whole synthesis
    run crashed. Now we record ``http_status_code=404`` and return [].
    """
    reset_outcomes()
    fake = _RoutedHttp({"efts.sec.gov": _FakeResp(status=404, text="Not Found")})
    adapter = Sec13FAdapter(http_client=fake)

    rows = await adapter.list_recent_13f(days=14)
    assert rows == []
    outcomes = [o for o in collect_outcomes() if o.adapter_name == "sec_13f"]
    assert len(outcomes) == 1, outcomes
    assert outcomes[0].status == "http_error"
    assert outcomes[0].http_status_code == 404
    assert outcomes[0].error_text is not None
    assert "404" in outcomes[0].error_text


@pytest.mark.asyncio
async def test_list_recent_13f_empty_hits_returns_empty(engine: None) -> None:
    """Valid JSON envelope with no hits → empty list, outcome=empty.

    A real-world case: a narrow ticker filter that no fund has filed on
    in the requested window. The adapter must not raise.
    """
    reset_outcomes()
    fake = _RoutedHttp(
        {"efts.sec.gov": _FakeResp(json_payload={"hits": {"hits": []}})}
    )
    adapter = Sec13FAdapter(http_client=fake)
    rows = await adapter.list_recent_13f(days=14, ticker="NOSUCHTICKER")
    assert rows == []
    # ``set_payload_size_bytes`` is still called with size of ``[]``
    # (== 2 bytes for "[]"), so status flips to "ok". The important
    # invariant is "no exception, no http_error, [] returned".
    outcomes = [o for o in collect_outcomes() if o.adapter_name == "sec_13f"]
    assert len(outcomes) == 1
    assert outcomes[0].status in ("ok", "empty")
    assert outcomes[0].http_status_code in (None, 0)


@pytest.mark.asyncio
async def test_list_recent_13f_ticker_normalization(engine: None) -> None:
    """Lowercase / mixed-case tickers are uppercased and quoted on the wire.

    EDGAR FTS tokenizes unquoted queries (so ``AA`` matches ``AAPL``,
    ``AAL``, etc.), so the adapter wraps the symbol in double-quotes
    for exact-phrase semantics. Lowercase comes from the UI / callers
    that don't normalize.
    """
    fake = _RoutedHttp({"efts.sec.gov": _FakeResp(json_payload=_FTS_PAYLOAD)})
    adapter = Sec13FAdapter(http_client=fake)
    await adapter.list_recent_13f(days=14, ticker="nvda")

    assert fake.calls_with_params, "expected at least one HTTP call"
    _url, params = fake.calls_with_params[0]
    # Quoted, uppercased.
    assert params.get("q") == '"NVDA"', params


def test_ticker_query_helper() -> None:
    """Pure helper coverage — exhaustive for the small surface."""
    assert _ticker_query(None) == ""
    assert _ticker_query("") == ""
    assert _ticker_query("   ") == ""
    assert _ticker_query("nvda") == '"NVDA"'
    assert _ticker_query("  NvDa  ") == '"NVDA"'


def test_extract_http_status_helper() -> None:
    """Pure helper coverage — round-trips the adapter's own error text."""
    err = MissingDataSourceError("SEC EDGAR returned HTTP 404 for https://x")
    assert _extract_http_status(err) == 404
    err5 = MissingDataSourceError("SEC EDGAR returned HTTP 503 for https://x")
    assert _extract_http_status(err5) == 503
    # Network-level error → no code embedded → None.
    err_n = MissingDataSourceError("SEC EDGAR unreachable (DNS failure); url=https://x")
    assert _extract_http_status(err_n) is None


@pytest.mark.asyncio
async def test_get_filer_history(engine: None) -> None:
    fake = _RoutedHttp(
        {"browse-edgar": _FakeResp(text=_BROWSE_ATOM, status=200)}
    )
    adapter = Sec13FAdapter(http_client=fake)
    rows = await adapter.get_filer_history("0001067983", quarters=2)
    assert len(rows) == 2
    assert rows[0]["accession_number"] == "0001067983-25-000002"


@pytest.mark.asyncio
async def test_get_filing_holdings(engine: None) -> None:
    routes = {
        "/index.json": _FakeResp(json_payload=_INDEX_JSON),
        "form13fInfoTable.xml": _FakeResp(text=_INFOTABLE_XML),
    }
    fake = _RoutedHttp(routes)
    adapter = Sec13FAdapter(http_client=fake)
    holdings = await adapter.get_filing_holdings("0001067983-25-000002")
    cusips = {h["cusip"] for h in holdings}
    assert cusips == {"037833100", "060505104"}


@pytest.mark.asyncio
async def test_get_filing_holdings_empty_accession(engine: None) -> None:
    adapter = Sec13FAdapter(http_client=_FailingHttp())
    with pytest.raises(ValueError):
        await adapter.get_filing_holdings("")


@pytest.mark.asyncio
async def test_get_filing_holdings_index_lookup_records_outcome_and_returns_empty(
    engine: None,
) -> None:
    """503 on the filing-index lookup → record outcome + [] (no raise).

    Same graceful-degradation contract as ``list_recent_13f``: one
    flaky filing must not crash the whole synthesis run. The outcome
    carries the structured 503 so the UI can show "sec_13f
    holdings:<acc>: HTTP 503".
    """
    reset_outcomes()
    routes = {"/index.json": _FakeResp(status=503, text="oops")}
    adapter = Sec13FAdapter(http_client=_RoutedHttp(routes))
    holdings = await adapter.get_filing_holdings("0001067983-25-000002")
    assert holdings == []
    outcomes = [o for o in collect_outcomes() if o.adapter_name == "sec_13f"]
    assert len(outcomes) == 1
    assert outcomes[0].status == "http_error"
    assert outcomes[0].http_status_code == 503
    assert outcomes[0].target == "holdings:0001067983-25-000002"


@pytest.mark.asyncio
async def test_get_filer_history_outage_records_outcome_and_returns_empty(
    engine: None,
) -> None:
    """Same graceful contract for ``get_filer_history``."""
    reset_outcomes()
    adapter = Sec13FAdapter(http_client=_FailingHttp())
    rows = await adapter.get_filer_history("0001067983", quarters=2)
    assert rows == []
    outcomes = [o for o in collect_outcomes() if o.adapter_name == "sec_13f"]
    assert len(outcomes) == 1
    assert outcomes[0].status == "http_error"
    assert outcomes[0].target == "history:0001067983"


@pytest.mark.asyncio
async def test_list_recent_13f_does_not_pin_host_header(engine: None) -> None:
    """Regression guard for the root cause of the original 404.

    Pinning ``Host: www.sec.gov`` made requests to ``efts.sec.gov`` route
    to an unknown CDN vhost and return 404. The fix: omit ``Host`` from
    the default headers and let httpx derive it per URL. This test
    asserts on the wire what the adapter is willing to send.
    """
    captured_headers: dict[str, str] = {}

    class _CapturingHttp:
        async def get(self, _url: str, **kwargs: Any) -> _FakeResp:
            hdrs = kwargs.get("headers") or {}
            captured_headers.update(hdrs)
            return _FakeResp(json_payload=_FTS_PAYLOAD)

    adapter = Sec13FAdapter(http_client=_CapturingHttp())
    await adapter.list_recent_13f(days=14)
    assert "User-Agent" in captured_headers, captured_headers
    assert "Argosy" in captured_headers["User-Agent"]
    assert "Host" not in captured_headers, (
        "Adapter must not pin a Host header — it breaks efts.sec.gov "
        "(the FTS endpoint) by routing to an unknown CDN vhost."
    )
