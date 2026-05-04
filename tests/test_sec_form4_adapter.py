"""SEC Form 4 (insider transactions) adapter tests."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.sec_form4_adapter import (
    TRANSACTION_CODE_MEANING,
    SecForm4Adapter,
    _filing_within_window,
    _parse_form4_atom_index,
    _parse_form4_xml,
    _parse_ticker_map,
)


# ----------------------------------------------------------------------
# Fixture XML / atom payloads
# ----------------------------------------------------------------------


_TICKERS_JSON = json.dumps(
    {
        "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
        "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    }
)


_FORM4_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>4 - HUANG JEN HSUN (0001045810)</title>
    <updated>{recent_date}T16:30:00-04:00</updated>
    <link href="https://www.sec.gov/Archives/edgar/data/1045810/0001045810-26-000001-index.htm"/>
  </entry>
  <entry>
    <title>4 - SOMEONE ELSE (0001045810)</title>
    <updated>{old_date}T16:30:00-04:00</updated>
    <link href="https://www.sec.gov/Archives/edgar/data/1045810/0001045810-25-000123-index.htm"/>
  </entry>
</feed>
""".format(
    recent_date=(date.today() - timedelta(days=2)).isoformat(),
    old_date=(date.today() - timedelta(days=400)).isoformat(),
)


_FORM4_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument>
  <issuer>
    <issuerCik>0001045810</issuerCik>
    <issuerName>NVIDIA CORP</issuerName>
    <issuerTradingSymbol>NVDA</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerName>HUANG JEN HSUN</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>President &amp; CEO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-04-15</value></transactionDate>
      <transactionCoding>
        <transactionCode>S</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>120000</value></transactionShares>
        <transactionPricePerShare><value>950.50</value></transactionPricePerShare>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>800000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-04-16</value></transactionDate>
      <transactionCoding>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>950.00</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


_INDEX_JSON = {
    "directory": {
        "item": [
            {"name": "0001045810-26-000001-index.htm"},
            {"name": "form4.xml"},
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
        json_payload: Any | None = None,
    ) -> None:
        self.status_code = status
        self.text = text or (json.dumps(json_payload) if json_payload is not None else "")
        self.content = self.text.encode("utf-8")
        self._json = json_payload

    def json(self) -> Any:
        if self._json is not None:
            return self._json
        return json.loads(self.text)


class _RoutedHttp:
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
# Pure-parsing tests
# ----------------------------------------------------------------------


def test_parse_ticker_map_indexed_dict() -> None:
    m = _parse_ticker_map(_TICKERS_JSON)
    assert m["NVDA"] == "0000001045810"[3:].zfill(10)
    assert m["NVDA"] == str(1045810).zfill(10)
    assert m["AAPL"] == str(320193).zfill(10)


def test_parse_ticker_map_malformed_raises() -> None:
    with pytest.raises(MissingDataSourceError):
        _parse_ticker_map("{not valid json")


def test_parse_form4_xml_extracts_transactions() -> None:
    rows = _parse_form4_xml(_FORM4_XML, accession="0001045810-26-000001")
    assert len(rows) == 2
    sale = rows[0]
    assert sale["ticker"] == "NVDA"
    assert sale["filer_name"] == "HUANG JEN HSUN"
    assert "officer" in sale["role"]
    assert sale["transaction_code"] == "S"
    assert sale["transaction_kind"] == "sale"
    assert sale["shares"] == 120000.0
    assert sale["price_per_share"] == pytest.approx(950.50)
    assert sale["value_usd"] == pytest.approx(120000.0 * 950.50)
    assert sale["post_transaction_holdings"] == 800000.0
    buy = rows[1]
    assert buy["transaction_code"] == "P"
    assert buy["transaction_kind"] == "purchase"


def test_parse_form4_xml_malformed_raises() -> None:
    with pytest.raises(MissingDataSourceError):
        _parse_form4_xml("<not-xml>", accession="x")


def test_parse_form4_atom_index() -> None:
    rows = _parse_form4_atom_index(_FORM4_ATOM, cik="0001045810")
    assert len(rows) == 2
    assert rows[0]["accession_number"] == "0001045810-26-000001"
    assert rows[0]["cik"] == "1045810"


def test_filing_within_window() -> None:
    cutoff = date.today() - timedelta(days=10)
    assert _filing_within_window(date.today().isoformat(), cutoff=cutoff)
    assert not _filing_within_window(
        (date.today() - timedelta(days=30)).isoformat(),
        cutoff=cutoff,
    )
    assert not _filing_within_window("", cutoff=cutoff)


def test_transaction_code_meaning_known_codes() -> None:
    assert TRANSACTION_CODE_MEANING["P"] == "purchase"
    assert TRANSACTION_CODE_MEANING["S"] == "sale"


# ----------------------------------------------------------------------
# Adapter tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_form4_for_ticker_happy_path(engine: None) -> None:
    routes = {
        "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
        "browse-edgar": _FakeResp(text=_FORM4_ATOM),
        "/index.json": _FakeResp(json_payload=_INDEX_JSON),
        "form4.xml": _FakeResp(text=_FORM4_XML),
    }
    fake = _RoutedHttp(routes)
    adapter = SecForm4Adapter(http_client=fake)
    rows = await adapter.get_recent_form4_for_ticker("NVDA", days=30)
    assert len(rows) == 2  # Only the recent atom-entry filing within window
    assert all(r["ticker"] == "NVDA" for r in rows)


@pytest.mark.asyncio
async def test_get_recent_form4_for_ticker_skips_old_filings(engine: None) -> None:
    routes = {
        "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
        "browse-edgar": _FakeResp(text=_FORM4_ATOM),
        "/index.json": _FakeResp(json_payload=_INDEX_JSON),
        "form4.xml": _FakeResp(text=_FORM4_XML),
    }
    fake = _RoutedHttp(routes)
    adapter = SecForm4Adapter(http_client=fake)
    # Tight 1-day window — recent (2 days old) atom entry is just *outside*.
    rows = await adapter.get_recent_form4_for_ticker("NVDA", days=1)
    assert rows == []


@pytest.mark.asyncio
async def test_get_recent_form4_for_ticker_unknown_ticker_raises(engine: None) -> None:
    routes = {"company_tickers.json": _FakeResp(text=_TICKERS_JSON)}
    adapter = SecForm4Adapter(http_client=_RoutedHttp(routes))
    with pytest.raises(MissingDataSourceError):
        await adapter.get_recent_form4_for_ticker("DOES_NOT_EXIST")


@pytest.mark.asyncio
async def test_get_recent_form4_for_filer(engine: None) -> None:
    routes = {
        "browse-edgar": _FakeResp(text=_FORM4_ATOM),
        "/index.json": _FakeResp(json_payload=_INDEX_JSON),
        "form4.xml": _FakeResp(text=_FORM4_XML),
    }
    adapter = SecForm4Adapter(http_client=_RoutedHttp(routes))
    rows = await adapter.get_recent_form4_for_filer("0001045810", days=30)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_outage_raises_missing_data_source(engine: None) -> None:
    adapter = SecForm4Adapter(http_client=_FailingHttp())
    with pytest.raises(MissingDataSourceError):
        await adapter.get_recent_form4_for_filer("0001045810")


@pytest.mark.asyncio
async def test_invalid_inputs_raise_value_error(engine: None) -> None:
    adapter = SecForm4Adapter(http_client=_FailingHttp())
    with pytest.raises(ValueError):
        await adapter.get_recent_form4_for_ticker("")
    with pytest.raises(ValueError):
        await adapter.get_recent_form4_for_ticker("NVDA", days=0)
    with pytest.raises(ValueError):
        await adapter.get_recent_form4_for_filer("")
    with pytest.raises(ValueError):
        await adapter.get_recent_form4_for_filer("123", days=-1)
