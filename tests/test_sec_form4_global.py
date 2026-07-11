"""Global SEC Form 4 daily-index collection and hardened XML parsing tests."""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Any

import pytest

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data import sec_form4_adapter as sec

_TICKERS_JSON = json.dumps(
    {
        "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
        "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    }
)

_OFFICIAL_FORM_INDEX_2026_07_09 = (
    "Description: Daily Index of EDGAR Dissemination Feed\n"
    "Last Data Received: July  9, 2026\n"
    "Comments: webmaster@sec.gov\n"
    "Anonymous FTP: ftp://ftp.sec.gov/edgar/\n"
    "\n"
    "Form Type   Company Name                                                  CIK\n"
    "      Date Filed  File Name\n"
    "--------------------------------------------------------------------------------"
    "----------------------\n"
    "4                PALANTIR TECHNOLOGIES INC.                                    "
    "1321655     20260709    edgar/data/1321655/0001321655-26-000111.txt\n"
    "4/A              PALANTIR TECHNOLOGIES INC.                                    "
    "1321655     20260709    edgar/data/1321655/0001321655-26-000112.txt\n"
    "8-K              NVIDIA CORP                                                   "
    "1045810     20260709    edgar/data/1045810/0001045810-26-000222.txt\n"
)

_SEC_AUTOMATION_BLOCK_HTML = """<!DOCTYPE html>
<html><head><title>Your Request Originates from an Undeclared Automated Tool</title></head>
<body>
<h1>Your Request Originates from an Undeclared Automated Tool</h1>
<p>Please declare your traffic by updating your user agent to include company specific information.</p>
</body></html>
"""


def _daily_index(*rows: tuple[str, str, str, str, str]) -> str:
    body = "\n".join(
        f"{form:<12}{company:<62}{cik:<12}{filed:<12}{filename}"
        for form, company, cik, filed, filename in rows
    )
    return (
        "Description: Daily Index of EDGAR Dissemination Feed\n"
        "Form Type   Company Name                                                  "
        "CIK         Date Filed  File Name\n"
        "--------------------------------------------------------------------------------"
        "----------------------\n"
        f"{body}\n"
    )


def _form4_xml(
    *,
    document_type: str = "4",
    shares: tuple[int, ...] = (100,),
    doc_10b5: bool = False,
    referenced_footnote: str = "",
    unrelated_footnote: str = "",
    remarks: str = "",
    issuer_cik: str = "0001045810",
    owner_cik: str = "0001234567",
    owner_name: str = "TEST INSIDER",
    period_of_report: str = "2026-07-10",
    date_of_original_submission: str = "",
    security_title: str = "Common Stock",
    direct_or_indirect_ownership: str = "D",
    nature_of_ownership: str = "",
) -> str:
    transactions: list[str] = []
    for index, share_count in enumerate(shares):
        reference = (
            f'<footnoteId id="{referenced_footnote}"/>'
            if referenced_footnote and index == 0
            else ""
        )
        transactions.append(
            f"""
    <nonDerivativeTransaction>
      <securityTitle><value>{security_title}</value></securityTitle>
      <transactionDate><value>2026-07-10</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{share_count}</value>{reference}</transactionShares>
        <transactionPricePerShare><value>10.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>{share_count + 1000}</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>{direct_or_indirect_ownership}</value></directOrIndirectOwnership>
        <natureOfOwnership><value>{nature_of_ownership}</value></natureOfOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>"""
        )
    footnotes: list[str] = []
    if referenced_footnote:
        footnotes.append(
            f'<footnote id="{referenced_footnote}">'
            "This purchase was made under a Rule 10b5 1 trading plan."
            "</footnote>"
        )
    if unrelated_footnote:
        footnotes.append(
            f'<footnote id="{unrelated_footnote}">'
            "Another transaction was made under a Rule 10B5—1 plan."
            "</footnote>"
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument>
  <documentType>{document_type}</documentType>
  <periodOfReport>{period_of_report}</periodOfReport>
  <dateOfOriginalSubmission>{date_of_original_submission}</dateOfOriginalSubmission>
  <aff10b5One>{str(doc_10b5).lower()}</aff10b5One>
  <issuer>
    <issuerCik>{issuer_cik}</issuerCik>
    <issuerName>NVIDIA CORP</issuerName>
    <issuerTradingSymbol>NVDA</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>{owner_cik}</rptOwnerCik>
      <rptOwnerName>{owner_name}</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>{''.join(transactions)}
  </nonDerivativeTable>
  <footnotes>{''.join(footnotes)}</footnotes>
  <remarks>{remarks}</remarks>
</ownershipDocument>
"""


class _FakeResp:
    def __init__(
        self,
        *,
        status: int = 200,
        text: str = "",
        json_payload: Any | None = None,
    ) -> None:
        self.status_code = status
        self.text = text or (
            json.dumps(json_payload) if json_payload is not None else ""
        )
        self.content = self.text.encode("utf-8")
        self._json = json_payload

    def json(self) -> Any:
        if self._json is not None:
            return self._json
        return json.loads(self.text)


class _Http:
    def __init__(
        self,
        routes: dict[str, _FakeResp],
        *,
        clock: _FakeClock | None = None,
    ) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.clock = clock
        self.starts: list[float] = []

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        self.calls.append(url)
        self.headers.append(dict(kwargs.get("headers") or {}))
        if self.clock is not None:
            self.starts.append(self.clock())
        for needle, response in self.routes.items():
            if needle in url:
                return response
        return _FakeResp(status=404, text="not found")


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def _adapter(http: _Http, *, clock: _FakeClock | None = None) -> sec.SecForm4Adapter:
    fake_clock = clock or _FakeClock()
    return sec.SecForm4Adapter(
        http_client=http,
        sleep=fake_clock.sleep,
        clock=fake_clock,
    )


def test_parse_daily_form_index_extracts_form4_and_amendments_only() -> None:
    text = _daily_index(
        (
            "4",
            "NVIDIA CORP",
            "1045810",
            "2026-07-10",
            "edgar/data/1045810/0001045810-26-000111.txt",
        ),
        (
            "4/A",
            "NVIDIA CORP",
            "1045810",
            "2026-07-11",
            "edgar/data/1045810/0001045810-26-000111-amend.txt",
        ),
        (
            "8-K",
            "NVIDIA CORP",
            "1045810",
            "2026-07-11",
            "edgar/data/1045810/other.txt",
        ),
    )

    rows = sec._parse_daily_form_index(text)

    assert [row["document_type"] for row in rows] == ["4", "4/A"]
    assert rows[0] == {
        "cik": "0001045810",
        "company_name": "NVIDIA CORP",
        "filed_at": "2026-07-10",
        "archive_filename": "edgar/data/1045810/0001045810-26-000111.txt",
        "accession": "0001045810-26-000111",
        "document_type": "4",
        "is_amendment": False,
    }
    assert rows[1]["is_amendment"] is True
    assert rows[1]["accession"] == "0001045810-26-000111-amend"


def test_parse_official_two_line_daily_index_and_normalize_compact_dates() -> None:
    rows = sec._parse_daily_form_index(_OFFICIAL_FORM_INDEX_2026_07_09)

    assert [row["document_type"] for row in rows] == ["4", "4/A"]
    assert [row["filed_at"] for row in rows] == ["2026-07-09", "2026-07-09"]
    assert rows[0]["company_name"] == "PALANTIR TECHNOLOGIES INC."
    assert rows[0]["accession"] == "0001321655-26-000111"


def test_parse_daily_form_index_rejects_malformed_response() -> None:
    with pytest.raises(MissingDataSourceError, match="daily Form index malformed"):
        sec._parse_daily_form_index("<html>rate limit page</html>")


def test_parse_ticker_maps_builds_both_directions() -> None:
    ticker_to_cik, cik_to_ticker = sec._parse_ticker_maps(_TICKERS_JSON)

    assert ticker_to_cik["NVDA"] == "0001045810"
    assert cik_to_ticker["0001045810"] == "NVDA"


def test_parse_form4_xml_carries_document_owner_and_transaction_fields() -> None:
    rows = sec._parse_form4_xml(
        _form4_xml(
            document_type="4/A",
            direct_or_indirect_ownership="I",
            nature_of_ownership="Family Trust",
        ),
        accession="acc",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["document_type"] == "4/A"
    assert row["is_amendment"] is True
    assert row["filer_cik"] == "0001234567"
    assert row["issuer_cik"] == "0001045810"
    assert row["security_title"] == "Common Stock"
    assert row["acquired_disposed_code"] == "A"
    assert row["transaction_index"] == 0
    assert row["period_of_report"] == "2026-07-10"
    assert row["date_of_original_submission"] == ""
    assert row["direct_or_indirect_ownership"] == "I"
    assert row["nature_of_ownership"] == "Family Trust"


def test_document_level_10b5_makes_unlinked_transactions_unknown() -> None:
    rows = sec._parse_form4_xml(_form4_xml(shares=(100, 200), doc_10b5=True))

    assert [row["is_10b5_1"] for row in rows] == [None, None]
    assert all(row["document_has_10b5_1"] is True for row in rows)
    assert all(
        "document:aff10b5One" in row["document_10b5_1_evidence"]
        for row in rows
    )


def test_only_referenced_10b5_footnote_marks_transaction_and_other_is_unknown() -> None:
    rows = sec._parse_form4_xml(
        _form4_xml(
            shares=(100, 200),
            doc_10b5=True,
            referenced_footnote="F1",
            unrelated_footnote="F2",
        )
    )

    assert rows[0]["is_10b5_1"] is True
    assert any("footnote:F1" in evidence for evidence in rows[0]["tenb5_1_evidence"])
    assert rows[1]["is_10b5_1"] is None
    assert rows[1]["tenb5_1_evidence"] == []


def test_unrelated_10b5_footnote_does_not_mark_transaction() -> None:
    rows = sec._parse_form4_xml(_form4_xml(unrelated_footnote="F9"))

    assert rows[0]["is_10b5_1"] is False


def test_remarks_10b5_match_is_case_and_punctuation_tolerant() -> None:
    rows = sec._parse_form4_xml(
        _form4_xml(remarks="Transactions were made under a 10B5—1 arrangement.")
    )

    assert rows[0]["is_10b5_1"] is None
    assert rows[0]["document_has_10b5_1"] is True
    assert any(
        evidence.startswith("remarks:")
        for evidence in rows[0]["document_10b5_1_evidence"]
    )


@pytest.mark.asyncio
async def test_global_fetch_uses_public_issuers_only(engine: None) -> None:
    day = date(2026, 7, 10)
    public_filer_cik = "1234567"
    private_filer_cik = "7654321"
    public_accession = "0001234567-26-000111"
    private_accession = "0007654321-26-000222"
    index = _daily_index(
        (
            "4",
            "TEST INSIDER",
            public_filer_cik,
            day.isoformat(),
            f"edgar/data/{public_filer_cik}/{public_accession}.txt",
        ),
        (
            "4",
            "PRIVATE INSIDER",
            private_filer_cik,
            day.isoformat(),
            f"edgar/data/{private_filer_cik}/{private_accession}.txt",
        ),
    )
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260710.idx": _FakeResp(text=index),
            f"/{public_filer_cik}/{public_accession.replace('-', '')}/index.json": _FakeResp(
                json_payload={"directory": {"item": [{"name": "form4.xml"}]}}
            ),
            f"/{public_filer_cik}/{public_accession.replace('-', '')}/form4.xml": _FakeResp(
                text=_form4_xml(
                    issuer_cik="0001045810",
                    owner_cik=public_filer_cik,
                )
            ),
            f"/{private_filer_cik}/{private_accession.replace('-', '')}/index.json": _FakeResp(
                json_payload={"directory": {"item": [{"name": "private.xml"}]}}
            ),
            f"/{private_filer_cik}/{private_accession.replace('-', '')}/private.xml": _FakeResp(
                text=_form4_xml(
                    issuer_cik="0009999999",
                    owner_cik=private_filer_cik,
                )
            ),
        }
    )

    rows = await _adapter(http).get_form4_for_date_range(
        day,
        day,
        ttl_seconds=0,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["filing_url"].endswith(
        f"/Archives/edgar/data/{public_filer_cik}/{public_accession}.txt"
    )
    assert row["filed_at"] == "2026-07-10"
    assert row["accession"] == public_accession
    assert row["filing_identity"] == public_accession
    assert row["filer_cik"] == public_filer_cik.zfill(10)
    assert row["issuer_cik"] == "0001045810"
    assert row["issuer_name"] == "NVIDIA CORP"
    assert row["ticker"] == "NVDA"
    assert any(f"/{private_filer_cik}/" in call for call in http.calls)


@pytest.mark.asyncio
async def test_global_fetch_skips_weekend_without_http(engine: None) -> None:
    day = date(2026, 7, 12)
    http = _Http({})

    assert (
        await _adapter(http).get_form4_for_date_range(day, day, ttl_seconds=0)
        == []
    )
    assert http.calls == []


@pytest.mark.asyncio
async def test_global_fetch_skips_observed_federal_holiday_without_http(
    engine: None,
) -> None:
    observed_independence_day = date(2026, 7, 3)
    http = _Http({})

    assert (
        await _adapter(http).get_form4_for_date_range(
            observed_independence_day,
            observed_independence_day,
            ttl_seconds=0,
        )
        == []
    )
    assert http.calls == []


def test_us_federal_holiday_observed_dates_and_regular_day() -> None:
    assert sec._is_us_federal_holiday(date(2026, 7, 3))  # July 4 is Saturday.
    assert sec._is_us_federal_holiday(date(2027, 7, 5))  # July 4 is Sunday.
    assert not sec._is_us_federal_holiday(date(2026, 7, 2))


@pytest.mark.asyncio
async def test_global_fetch_business_day_404_fails_and_is_not_cached(
    engine: None,
) -> None:
    day = date(2026, 7, 10)
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260710.idx": _FakeResp(status=404),
        }
    )
    adapter = _adapter(http)

    for _ in range(2):
        with pytest.raises(MissingDataSourceError, match="HTTP 404"):
            await adapter.get_form4_for_date_range(day, day)

    assert sum(call.endswith("form.20260710.idx") for call in http.calls) == 2


@pytest.mark.asyncio
async def test_global_fetch_business_day_403_fails_loudly(engine: None) -> None:
    day = date(2026, 7, 9)
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260709.idx": _FakeResp(status=403),
        }
    )

    with pytest.raises(MissingDataSourceError, match="HTTP 403"):
        await _adapter(http).get_form4_for_date_range(day, day, ttl_seconds=0)


@pytest.mark.asyncio
async def test_daily_index_detects_sec_automation_block_at_http_200() -> None:
    day = date(2026, 7, 9)
    http = _Http(
        {"form.20260709.idx": _FakeResp(text=_SEC_AUTOMATION_BLOCK_HTML)}
    )

    with pytest.raises(
        MissingDataSourceError,
        match=r"automation block.*contact User-Agent",
    ):
        await _adapter(http)._fetch_daily_form_index(day)


@pytest.mark.asyncio
async def test_form4_requests_use_runtime_sec_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARGOSY_SEC_CONTACT_EMAIL", "sec-contact@example.com")
    http = _Http({"/one": _FakeResp(text="ok")})

    await _adapter(http)._fetch_text("https://www.sec.gov/one")

    assert http.headers[0]["User-Agent"].endswith(" sec-contact@example.com")


@pytest.mark.asyncio
async def test_default_through_avoids_current_unpublished_index(engine: None) -> None:
    today = date(2026, 7, 13)
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            ".idx": _FakeResp(status=404),
        }
    )
    clock = _FakeClock()
    adapter = sec.SecForm4Adapter(
        http_client=http,
        sleep=clock.sleep,
        clock=clock,
        today=lambda: today,
    )

    with pytest.raises(MissingDataSourceError, match="HTTP 404"):
        await adapter.get_form4_for_date_range(today - timedelta(days=3), ttl_seconds=0)

    assert not any(call.endswith("form.20260713.idx") for call in http.calls)


@pytest.mark.asyncio
async def test_global_fetch_daily_index_5xx_fails_loudly(engine: None) -> None:
    day = date(2026, 7, 10)
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260710.idx": _FakeResp(status=503),
        }
    )

    with pytest.raises(MissingDataSourceError, match="HTTP 503"):
        await _adapter(http).get_form4_for_date_range(day, day, ttl_seconds=0)


@pytest.mark.asyncio
async def test_global_fetch_rejects_malformed_daily_index(engine: None) -> None:
    day = date(2026, 7, 10)
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260710.idx": _FakeResp(text="<html>not an index</html>"),
        }
    )

    with pytest.raises(MissingDataSourceError, match="daily Form index malformed"):
        await _adapter(http).get_form4_for_date_range(day, day, ttl_seconds=0)


@pytest.mark.asyncio
async def test_amendment_supersedes_without_collapsing_same_day_positions(
    engine: None,
) -> None:
    day_one = date(2026, 7, 10)
    day_two = date(2026, 7, 13)
    filer_cik = "1234567"
    original_accession = "0001234567-26-000111"
    amendment_accession = "0001234567-26-000112"
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260710.idx": _FakeResp(
                text=_daily_index(
                    (
                        "4",
                        "TEST INSIDER",
                        filer_cik,
                        day_one.isoformat(),
                        f"edgar/data/{filer_cik}/{original_accession}.txt",
                    )
                )
            ),
            f"form.{day_two:%Y%m%d}.idx": _FakeResp(
                text=_daily_index(
                    (
                        "4/A",
                        "TEST INSIDER",
                        filer_cik,
                        day_two.isoformat(),
                        f"edgar/data/{filer_cik}/{amendment_accession}.txt",
                    )
                )
            ),
            f"/{original_accession.replace('-', '')}/index.json": _FakeResp(
                json_payload={"directory": {"item": [{"name": "original.xml"}]}}
            ),
            f"/{original_accession.replace('-', '')}/original.xml": _FakeResp(
                text=_form4_xml(
                    document_type="4",
                    shares=(100, 200),
                    owner_cik=filer_cik,
                )
            ),
            f"/{amendment_accession.replace('-', '')}/index.json": _FakeResp(
                json_payload={"directory": {"item": [{"name": "amendment.xml"}]}}
            ),
            f"/{amendment_accession.replace('-', '')}/amendment.xml": _FakeResp(
                text=_form4_xml(
                    document_type="4/A",
                    shares=(200, 101),
                    owner_cik=filer_cik,
                    date_of_original_submission=day_one.isoformat(),
                )
            ),
        }
    )

    rows = await _adapter(http).get_form4_for_date_range(
        day_one,
        day_two,
        ttl_seconds=0,
    )

    assert len(rows) == 2
    assert [row["transaction_index"] for row in rows] == [0, 1]
    assert [row["shares"] for row in rows] == [200.0, 101.0]
    assert all(row["is_amendment"] is True for row in rows)
    assert all(len(row["source_urls"]) == 2 for row in rows)
    assert all(row["accession"] == amendment_accession for row in rows)
    assert all(row["filing_identity"] == original_accession for row in rows)
    assert all(row["amendment_match_status"] == "matched" for row in rows)


@pytest.mark.asyncio
async def test_distinct_nonamended_same_day_accessions_remain_distinct(
    engine: None,
) -> None:
    day = date(2026, 7, 10)
    filer_cik = "1234567"
    accession_one = "0001234567-26-000201"
    accession_two = "0001234567-26-000202"
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260710.idx": _FakeResp(
                text=_daily_index(
                    (
                        "4",
                        "TEST INSIDER",
                        filer_cik,
                        day.isoformat(),
                        f"edgar/data/{filer_cik}/{accession_one}.txt",
                    ),
                    (
                        "4",
                        "TEST INSIDER",
                        filer_cik,
                        day.isoformat(),
                        f"edgar/data/{filer_cik}/{accession_two}.txt",
                    ),
                )
            ),
            f"/{accession_one.replace('-', '')}/index.json": _FakeResp(
                json_payload={"directory": {"item": [{"name": "one.xml"}]}}
            ),
            f"/{accession_one.replace('-', '')}/one.xml": _FakeResp(
                text=_form4_xml(shares=(100,), owner_cik=filer_cik)
            ),
            f"/{accession_two.replace('-', '')}/index.json": _FakeResp(
                json_payload={"directory": {"item": [{"name": "two.xml"}]}}
            ),
            f"/{accession_two.replace('-', '')}/two.xml": _FakeResp(
                text=_form4_xml(shares=(200,), owner_cik=filer_cik)
            ),
        }
    )

    rows = await _adapter(http).get_form4_for_date_range(day, day, ttl_seconds=0)

    assert len(rows) == 2
    assert {row["accession"] for row in rows} == {accession_one, accession_two}
    assert {row["filing_identity"] for row in rows} == {
        accession_one,
        accession_two,
    }


@pytest.mark.asyncio
async def test_ambiguous_amendment_excludes_originals_and_marks_evidence(
    engine: None,
) -> None:
    day_one = date(2026, 7, 10)
    day_two = date(2026, 7, 13)
    filer_cik = "1234567"
    original_one = "0001234567-26-000301"
    original_two = "0001234567-26-000302"
    amendment = "0001234567-26-000303"
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260710.idx": _FakeResp(
                text=_daily_index(
                    (
                        "4",
                        "TEST INSIDER",
                        filer_cik,
                        day_one.isoformat(),
                        f"edgar/data/{filer_cik}/{original_one}.txt",
                    ),
                    (
                        "4",
                        "TEST INSIDER",
                        filer_cik,
                        day_one.isoformat(),
                        f"edgar/data/{filer_cik}/{original_two}.txt",
                    ),
                )
            ),
            "form.20260713.idx": _FakeResp(
                text=_daily_index(
                    (
                        "4/A",
                        "TEST INSIDER",
                        filer_cik,
                        day_two.isoformat(),
                        f"edgar/data/{filer_cik}/{amendment}.txt",
                    )
                )
            ),
            f"/{original_one.replace('-', '')}/index.json": _FakeResp(
                json_payload={"directory": {"item": [{"name": "one.xml"}]}}
            ),
            f"/{original_one.replace('-', '')}/one.xml": _FakeResp(
                text=_form4_xml(shares=(100,), owner_cik=filer_cik)
            ),
            f"/{original_two.replace('-', '')}/index.json": _FakeResp(
                json_payload={"directory": {"item": [{"name": "two.xml"}]}}
            ),
            f"/{original_two.replace('-', '')}/two.xml": _FakeResp(
                text=_form4_xml(shares=(200,), owner_cik=filer_cik)
            ),
            f"/{amendment.replace('-', '')}/index.json": _FakeResp(
                json_payload={"directory": {"item": [{"name": "amend.xml"}]}}
            ),
            f"/{amendment.replace('-', '')}/amend.xml": _FakeResp(
                text=_form4_xml(
                    document_type="4/A",
                    shares=(300,),
                    owner_cik=filer_cik,
                    date_of_original_submission=day_one.isoformat(),
                )
            ),
            "form.20260711.idx": _FakeResp(status=404),
            "form.20260712.idx": _FakeResp(status=404),
        }
    )

    rows = await _adapter(http).get_form4_for_date_range(
        day_one,
        day_two,
        ttl_seconds=0,
    )

    assert len(rows) == 1
    assert rows[0]["accession"] == amendment
    assert rows[0]["filing_identity"] == amendment
    assert rows[0]["cluster_eligible"] is False
    assert rows[0]["amendment_match_status"] == "ambiguous"
    assert rows[0]["amendment_ambiguity_evidence"] == [
        original_one,
        original_two,
    ]


def test_unmatched_amendment_uses_own_explicit_ineligible_identity() -> None:
    amendment_accession = "0001234567-26-000401"
    filing_url = f"https://www.sec.gov/Archives/{amendment_accession}.txt"
    row = sec._parse_form4_xml(
        _form4_xml(
            document_type="4/A",
            date_of_original_submission="2026-07-09",
        ),
        accession=amendment_accession,
    )[0]
    row.update({"filed_at": "2026-07-13", "filing_url": filing_url})
    rows = sec._select_filing_versions(
        [
            {
                "accession": amendment_accession,
                "filed_at": "2026-07-13",
                "filing_url": filing_url,
                "is_amendment": True,
                "issuer_cik": row["issuer_cik"],
                "filer_cik": row["filer_cik"],
                "filer_name": row["filer_name"],
                "date_of_original_submission": "2026-07-09",
                "rows": [row],
            }
        ]
    )

    assert rows[0]["accession"] == amendment_accession
    assert rows[0]["filing_identity"] == amendment_accession
    assert rows[0]["amendment_match_status"] == "unmatched"
    assert rows[0]["cluster_eligible"] is False
    assert rows[0]["source_urls"] == [filing_url]


@pytest.mark.asyncio
async def test_all_instances_share_fair_access_pacing() -> None:
    clock = _FakeClock()
    http = _Http(
        {
            "/one": _FakeResp(text="one"),
            "/two": _FakeResp(text="two"),
        },
        clock=clock,
    )
    first = _adapter(http, clock=clock)
    second = _adapter(http, clock=clock)

    await asyncio.gather(
        first._fetch_text("https://www.sec.gov/one"),
        second._fetch_text("https://www.sec.gov/two"),
    )

    assert len(http.starts) == 2
    assert http.starts[1] - http.starts[0] >= 0.11


@pytest.mark.asyncio
async def test_global_date_range_is_bounded_and_ordered(engine: None) -> None:
    start = date(2026, 7, 10)
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            ".idx": _FakeResp(text=_daily_index()),
        }
    )
    adapter = _adapter(http)

    with pytest.raises(ValueError, match="start_date"):
        await adapter.get_form4_for_date_range(start, start - timedelta(days=1))
    with pytest.raises(ValueError, match="at most"):
        await adapter.get_form4_for_date_range(start, start + timedelta(days=45))

    await adapter.get_form4_for_date_range(start, start + timedelta(days=2), ttl_seconds=0)
    daily_calls = [call for call in http.calls if call.endswith(".idx")]
    assert daily_calls == sorted(daily_calls)
