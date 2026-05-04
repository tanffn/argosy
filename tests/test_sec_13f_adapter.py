"""SEC 13F adapter tests — pure parsers + DI'd HTTP.

We never hit the network; every test patches the adapter's `http_client`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.sec_13f_adapter import (
    Sec13FAdapter,
    _accession_dashed,
    _parse_browse_atom,
    _parse_fts_hits,
    _parse_information_table_xml,
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
    """Routes specific URL substrings to canned responses; defaults to 404."""

    def __init__(self, routes: dict[str, _FakeResp]) -> None:
        self._routes = routes
        self.calls: list[str] = []

    async def get(self, url: str, **_kwargs: Any) -> _FakeResp:
        self.calls.append(url)
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
async def test_list_recent_13f_invalid_days(engine: None) -> None:
    adapter = Sec13FAdapter(http_client=_FailingHttp())
    with pytest.raises(ValueError):
        await adapter.list_recent_13f(days=0)


@pytest.mark.asyncio
async def test_list_recent_13f_outage_raises(engine: None) -> None:
    adapter = Sec13FAdapter(http_client=_FailingHttp())
    with pytest.raises(MissingDataSourceError):
        await adapter.list_recent_13f(days=30)


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
async def test_get_filing_holdings_index_lookup_fails(engine: None) -> None:
    routes = {"/index.json": _FakeResp(status=503, text="oops")}
    adapter = Sec13FAdapter(http_client=_RoutedHttp(routes))
    with pytest.raises(MissingDataSourceError):
        await adapter.get_filing_holdings("0001067983-25-000002")
