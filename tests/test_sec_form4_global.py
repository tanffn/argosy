"""Global SEC Form 4 daily-index collection and hardened XML parsing tests."""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Any

import pytest

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data import sec_form4_adapter as sec


@pytest.fixture(autouse=True)
def _declared_sec_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGOSY_SEC_CONTACT_EMAIL", "tests@example.com")


_TICKERS_JSON = json.dumps(
    {
        "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
        "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "2": {
            "cik_str": 1321655,
            "ticker": "PLTR",
            "title": "PALANTIR TECHNOLOGIES INC.",
        },
        "3": {
            "cik_str": 1726711,
            "ticker": "PUB",
            "title": "PUBLIC ISSUER INC.",
        },
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
    issuer_ticker: str = "NVDA",
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
    <issuerTradingSymbol>{issuer_ticker}</issuerTradingSymbol>
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


def _full_submission(xml: str) -> str:
    return (
        "<SEC-DOCUMENT>0000000000-26-000001.txt\n"
        "<DOCUMENT>\n<TYPE>4\n<SEQUENCE>1\n<FILENAME>form4.xml\n<TEXT>\n"
        f"{xml}"
        "\n</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>"
    )


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


class _ConcurrencyHttp(_Http):
    def __init__(
        self,
        routes: dict[str, _FakeResp],
        *,
        clock: _FakeClock,
        delays: dict[str, float],
    ) -> None:
        super().__init__(routes, clock=clock)
        self.delays = delays
        self.active_filings = 0
        self.max_active_filings = 0

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        self.calls.append(url)
        self.headers.append(dict(kwargs.get("headers") or {}))
        self.starts.append(self.clock())
        if url.endswith(".txt"):
            self.active_filings += 1
            self.max_active_filings = max(
                self.max_active_filings,
                self.active_filings,
            )
            try:
                for needle, delay in self.delays.items():
                    if needle in url:
                        await asyncio.sleep(delay)
                        break
            finally:
                self.active_filings -= 1
        for needle, response in self.routes.items():
            if needle in url:
                return response
        return _FakeResp(status=404, text="not found")


class _CancellationHttp(_Http):
    def __init__(
        self,
        routes: dict[str, _FakeResp],
        *,
        clock: _FakeClock,
        accessions: list[str],
    ) -> None:
        super().__init__(routes, clock=clock)
        self.accessions = accessions
        self.blocked_started = asyncio.Event()
        self.blocked_cancelled = False

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:
        if self.accessions[0] in url:
            self.calls.append(url)
            self.starts.append(self.clock())
            await self.blocked_started.wait()
            raise OSError("simulated filing failure")
        if self.accessions[1] in url:
            self.calls.append(url)
            self.starts.append(self.clock())
            self.blocked_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.blocked_cancelled = True
                raise
        return await super().get(url, **kwargs)


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def _adapter(
    http: _Http,
    *,
    clock: _FakeClock | None = None,
    max_concurrent_filing_fetches: int = 8,
) -> sec.SecForm4Adapter:
    fake_clock = clock or _FakeClock()
    return sec.SecForm4Adapter(
        http_client=http,
        sleep=fake_clock.sleep,
        clock=fake_clock,
        max_concurrent_filing_fetches=max_concurrent_filing_fetches,
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


def test_ticker_maps_preserve_all_share_classes_per_issuer() -> None:
    ticker_to_cik, cik_to_tickers = sec._parse_ticker_maps(
        json.dumps(
            {
                "0": {"cik_str": 1652044, "ticker": "GOOG"},
                "1": {"cik_str": 1652044, "ticker": "GOOGL"},
            }
        )
    )

    assert ticker_to_cik == {
        "GOOG": "0001652044",
        "GOOGL": "0001652044",
    }
    assert cik_to_tickers == {
        "0001652044": ("GOOG", "GOOGL"),
    }


def test_class_symbol_aliases_cover_delimiters_only_for_class_suffixes() -> None:
    from argosy.services.ticker_aliases import equivalent_class_symbols

    expected = ("BRK-B", "BRK.B", "BRK/B")
    assert equivalent_class_symbols("brk.b") == expected
    assert equivalent_class_symbols("BRK/B") == expected
    assert equivalent_class_symbols("BRK-B") == expected
    assert equivalent_class_symbols("ABC-DEF") == ("ABC-DEF",)
    assert equivalent_class_symbols("AAPL") == ("AAPL",)


@pytest.mark.asyncio
async def test_global_collector_canonicalizes_brk_class_alias_and_preserves_reported(
    engine: None,
) -> None:
    day = date(2026, 7, 10)
    cik = "1067983"
    accession = "0001067983-26-000111"
    tickers = json.dumps(
        {
            "0": {"cik_str": 1067983, "ticker": "BRK-B"},
            "1": {"cik_str": 9999999, "ticker": "OTHER"},
        }
    )
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=tickers),
            "form.20260710.idx": _FakeResp(
                text=_daily_index(
                    (
                        "4",
                        "BERKSHIRE HATHAWAY INC.",
                        cik,
                        day.isoformat(),
                        f"edgar/data/{cik}/{accession}.txt",
                    )
                )
            ),
            accession: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        issuer_cik=cik,
                        issuer_ticker="BRK.B",
                    )
                )
            ),
        }
    )

    rows = await _adapter(http).get_form4_for_date_range(
        day,
        day,
        ttl_seconds=0,
    )

    assert {row["ticker"] for row in rows} == {"BRK-B"}
    assert {row["reported_ticker"] for row in rows} == {"BRK.B"}


@pytest.mark.asyncio
async def test_global_collector_uses_exact_parsed_multi_class_symbol(
    engine: None,
) -> None:
    day = date(2026, 7, 10)
    cik = "1652044"
    accession = "0001652044-26-000111"
    tickers = json.dumps(
        {
            "0": {"cik_str": 1652044, "ticker": "GOOG"},
            "1": {"cik_str": 1652044, "ticker": "GOOGL"},
        }
    )
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=tickers),
            "form.20260710.idx": _FakeResp(
                text=_daily_index(
                    (
                        "4",
                        "ALPHABET INC.",
                        cik,
                        day.isoformat(),
                        f"edgar/data/{cik}/{accession}.txt",
                    )
                )
            ),
            accession: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        issuer_cik=cik,
                        issuer_ticker="GOOGL",
                    )
                )
            ),
        }
    )

    rows = await _adapter(http).get_form4_for_date_range(
        day,
        day,
        ttl_seconds=0,
    )

    assert {row["ticker"] for row in rows} == {"GOOGL"}


@pytest.mark.asyncio
async def test_global_collector_skips_ambiguous_multi_class_without_symbol(
    engine: None,
) -> None:
    day = date(2026, 7, 10)
    cik = "1652044"
    accession = "0001652044-26-000112"
    tickers = json.dumps(
        {
            "0": {"cik_str": 1652044, "ticker": "GOOG"},
            "1": {"cik_str": 1652044, "ticker": "GOOGL"},
        }
    )
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=tickers),
            "form.20260710.idx": _FakeResp(
                text=_daily_index(
                    (
                        "4",
                        "ALPHABET INC.",
                        cik,
                        day.isoformat(),
                        f"edgar/data/{cik}/{accession}.txt",
                    )
                )
            ),
            accession: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        issuer_cik=cik,
                        issuer_ticker="",
                    )
                )
            ),
        }
    )

    rows = await _adapter(http).get_form4_for_date_range(
        day,
        day,
        ttl_seconds=0,
    )

    assert rows == []


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
    ticker_to_cik, cik_to_tickers = sec._parse_ticker_maps(_TICKERS_JSON)

    assert ticker_to_cik["NVDA"] == "0001045810"
    assert cik_to_tickers["0001045810"] == ("NVDA",)


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


def test_extract_ownership_document_from_full_submission() -> None:
    xml = _form4_xml(owner_cik="0001234567", issuer_cik="0001045810")

    extracted = sec._extract_ownership_document(_full_submission(xml))

    assert extracted.startswith("<ownershipDocument>")
    assert extracted.endswith("</ownershipDocument>")
    assert "<rptOwnerCik>0001234567</rptOwnerCik>" in extracted


@pytest.mark.parametrize(
    "submission",
    [
        "<SEC-DOCUMENT><DOCUMENT><TYPE>4</DOCUMENT></SEC-DOCUMENT>",
        "<ownershipDocument><issuer></issuer>",
        (
            "<ownershipDocument></ownershipDocument>"
            "<ownershipDocument></ownershipDocument>"
        ),
    ],
)
def test_extract_ownership_document_rejects_missing_or_ambiguous_bounds(
    submission: str,
) -> None:
    with pytest.raises(MissingDataSourceError, match="ownershipDocument"):
        sec._extract_ownership_document(submission)


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
    public_issuer_cik = "1045810"
    private_issuer_cik = "9999999"
    public_owner_cik = "1234567"
    public_accession = "0001234567-26-000111"
    private_accession = "0007654321-26-000222"
    index = _daily_index(
        (
            "4",
            "NVIDIA CORP",
            public_issuer_cik,
            day.isoformat(),
            f"edgar/data/{public_issuer_cik}/{public_accession}.txt",
        ),
        (
            "4",
            "PRIVATE ISSUER",
            private_issuer_cik,
            day.isoformat(),
            f"edgar/data/{private_issuer_cik}/{private_accession}.txt",
        ),
    )
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260710.idx": _FakeResp(text=index),
            f"/edgar/data/{public_issuer_cik}/{public_accession}.txt": _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        issuer_cik=public_issuer_cik,
                        owner_cik=public_owner_cik,
                    )
                )
            ),
            private_accession: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        issuer_cik=private_issuer_cik,
                        owner_cik="7654321",
                    )
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
        f"/Archives/edgar/data/{public_issuer_cik}/{public_accession}.txt"
    )
    assert row["filed_at"] == "2026-07-10"
    assert row["accession"] == public_accession
    assert row["filing_identity"] == public_accession
    assert row["filer_cik"] == public_owner_cik.zfill(10)
    assert row["issuer_cik"] == "0001045810"
    assert row["issuer_name"] == "NVIDIA CORP"
    assert row["ticker"] == "NVDA"
    assert row["index_cik"] == "0001045810"
    assert any(f"/{private_issuer_cik}/" in call for call in http.calls)
    assert sum(call.endswith(".txt") for call in http.calls) == 2
    assert not any("index.json" in call for call in http.calls)


@pytest.mark.asyncio
async def test_official_index_cik_is_issuer_while_xml_cik_is_owner(
    engine: None,
) -> None:
    accessions = [
        "0001321655-26-000111",
        "0001321655-26-000112",
    ]
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260709.idx": _FakeResp(
                text=_OFFICIAL_FORM_INDEX_2026_07_09
            ),
            accessions[0]: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        issuer_cik="1321655",
                            issuer_ticker="PLTR",
                        owner_cik="1234567",
                    )
                )
            ),
            accessions[1]: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        document_type="4/A",
                        issuer_cik="1321655",
                            issuer_ticker="PLTR",
                        owner_cik="7654321",
                        date_of_original_submission="2026-07-09",
                    )
                )
            ),
        }
    )

    rows = await _adapter(http).get_form4_for_date_range(
        date(2026, 7, 9),
        date(2026, 7, 9),
        ttl_seconds=0,
    )

    assert {row["issuer_cik"] for row in rows} == {"0001321655"}
    assert {row["filer_cik"] for row in rows} == {
        "0001234567",
        "0007654321",
    }
    assert {row["ticker"] for row in rows} == {"PLTR"}


@pytest.mark.asyncio
async def test_single_index_filing_is_fetched_for_joint_owner_discovery(
    engine: None,
) -> None:
    day = date(2026, 7, 10)
    accession = "0001234567-26-000111"
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260710.idx": _FakeResp(
                text=_daily_index(
                    (
                        "4",
                        "NVIDIA CORP",
                        "1045810",
                        day.isoformat(),
                        f"edgar/data/1045810/{accession}.txt",
                    )
                )
            ),
            accession: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        issuer_cik="1045810",
                        owner_cik="1234567",
                    )
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
    assert sum(call.endswith(".txt") for call in http.calls) == 1


@pytest.mark.asyncio
async def test_global_fetch_cost_is_one_full_submission_per_index_filing(
    engine: None,
) -> None:
    day = date(2026, 7, 10)
    accessions = [
        "0001234567-26-000111",
        "0007654321-26-000222",
    ]
    index = _daily_index(
        *[
            (
                "4",
                "NVIDIA CORP",
                "1045810",
                day.isoformat(),
                f"edgar/data/1045810/{accession}.txt",
            )
            for accession in accessions
        ]
    )
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260710.idx": _FakeResp(text=index),
            accessions[0]: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        issuer_cik="1045810",
                        owner_cik="1234567",
                    )
                )
            ),
            accessions[1]: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        issuer_cik="1045810",
                        owner_cik="7654321",
                    )
                )
            ),
        }
    )

    rows = await _adapter(http).get_form4_for_date_range(
        day,
        day,
        ttl_seconds=0,
    )

    submission_calls = [call for call in http.calls if call.endswith(".txt")]
    assert len(submission_calls) == len(accessions)
    assert not any("index.json" in call for call in http.calls)
    assert [row["accession"] for row in rows] == accessions
    assert {row["filer_cik"] for row in rows} == {
        "0001234567",
        "0007654321",
    }
    assert {row["issuer_cik"] for row in rows} == {"0001045810"}
    assert {row["ticker"] for row in rows} == {"NVDA"}


@pytest.mark.asyncio
async def test_index_reporting_owner_cik_resolves_parsed_public_issuer(
    engine: None,
) -> None:
    day = date(2026, 7, 10)
    accession = "0000070858-26-000336"
    http = _Http(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260710.idx": _FakeResp(
                text=_daily_index(
                    (
                        "4",
                        "REPORTING OWNER",
                        "70858",
                        day.isoformat(),
                        f"edgar/data/1726711/{accession}.txt",
                    )
                )
            ),
            accession: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        issuer_cik="1726711",
                            issuer_ticker="PUB",
                        owner_cik="70858",
                    )
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
    assert rows[0]["index_cik"] == "0000070858"
    assert rows[0]["filer_cik"] == "0000070858"
    assert rows[0]["issuer_cik"] == "0001726711"
    assert rows[0]["ticker"] == "PUB"
    assert rows[0]["filing_url"].endswith(
        "/Archives/edgar/data/1726711/0000070858-26-000336.txt"
    )


@pytest.mark.asyncio
async def test_global_filing_fetches_are_bounded_paced_and_deterministic(
    engine: None,
) -> None:
    day = date(2026, 7, 10)
    accessions = [f"0001234567-26-{index:06d}" for index in range(1, 6)]
    index = _daily_index(
        *[
            (
                "4",
                "NVIDIA CORP",
                "1045810",
                day.isoformat(),
                f"edgar/data/1045810/{accession}.txt",
            )
            for accession in accessions
        ]
    )
    routes = {
        "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
        "form.20260710.idx": _FakeResp(text=index),
        **{
            accession: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        issuer_cik="1045810",
                        owner_cik=str(2_000_000 + position),
                    )
                )
            )
            for position, accession in enumerate(accessions)
        },
    }
    clock = _FakeClock()
    http = _ConcurrencyHttp(
        routes,
        clock=clock,
        delays={
            accession: (len(accessions) - position) / 1000
            for position, accession in enumerate(accessions)
        },
    )

    rows = await _adapter(
        http,
        clock=clock,
        max_concurrent_filing_fetches=2,
    ).get_form4_for_date_range(day, day, ttl_seconds=0)

    assert http.max_active_filings == 2
    start_gaps = [
        later - earlier
        for earlier, later in zip(http.starts, http.starts[1:], strict=False)
    ]
    assert all(gap >= 0.11 - 1e-12 for gap in start_gaps), (
        http.starts,
        start_gaps,
    )
    assert [row["accession"] for row in rows] == accessions


@pytest.mark.asyncio
async def test_global_worker_failure_cancels_siblings_and_stops_new_requests(
    engine: None,
) -> None:
    day = date(2026, 7, 10)
    accessions = [f"0001234567-26-{index:06d}" for index in range(1, 4)]
    index = _daily_index(
        *[
            (
                "4",
                "NVIDIA CORP",
                "1045810",
                day.isoformat(),
                f"edgar/data/1045810/{accession}.txt",
            )
            for accession in accessions
        ]
    )
    clock = _FakeClock()
    http = _CancellationHttp(
        {
            "company_tickers.json": _FakeResp(text=_TICKERS_JSON),
            "form.20260710.idx": _FakeResp(text=index),
        },
        clock=clock,
        accessions=accessions,
    )
    adapter = _adapter(
        http,
        clock=clock,
        max_concurrent_filing_fetches=2,
    )

    with pytest.raises(MissingDataSourceError, match="simulated filing failure"):
        await adapter.get_form4_for_date_range(day, day, ttl_seconds=0)

    await asyncio.sleep(0)
    assert http.blocked_cancelled is True
    assert not any(accessions[2] in call for call in http.calls)


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
@pytest.mark.parametrize(
    "contact",
    [None, "   ", "ops@argosy.local"],
)
async def test_form4_request_refuses_invalid_sec_contact_before_network(
    monkeypatch: pytest.MonkeyPatch,
    contact: str | None,
) -> None:
    if contact is None:
        monkeypatch.delenv("ARGOSY_SEC_CONTACT_EMAIL", raising=False)
    else:
        monkeypatch.setenv("ARGOSY_SEC_CONTACT_EMAIL", contact)
    http = _Http({"/one": _FakeResp(text="ok")})

    with pytest.raises(
        (ValueError, MissingDataSourceError),
        match="ARGOSY_SEC_CONTACT_EMAIL",
    ):
        await _adapter(http)._fetch_text("https://www.sec.gov/one")

    assert http.calls == []


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
    issuer_cik = "1045810"
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
                        "NVIDIA CORP",
                        issuer_cik,
                        day_one.isoformat(),
                        f"edgar/data/{issuer_cik}/{original_accession}.txt",
                    )
                )
            ),
            f"form.{day_two:%Y%m%d}.idx": _FakeResp(
                text=_daily_index(
                    (
                        "4/A",
                        "NVIDIA CORP",
                        issuer_cik,
                        day_two.isoformat(),
                        f"edgar/data/{issuer_cik}/{amendment_accession}.txt",
                    )
                )
            ),
            original_accession: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        document_type="4",
                        shares=(100, 200),
                        issuer_cik=issuer_cik,
                        owner_cik=filer_cik,
                    )
                )
            ),
            amendment_accession: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        document_type="4/A",
                        shares=(200, 101),
                        issuer_cik=issuer_cik,
                        owner_cik=filer_cik,
                        date_of_original_submission=day_one.isoformat(),
                    )
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
    issuer_cik = "1045810"
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
                        "NVIDIA CORP",
                        issuer_cik,
                        day.isoformat(),
                        f"edgar/data/{issuer_cik}/{accession_one}.txt",
                    ),
                    (
                        "4",
                        "NVIDIA CORP",
                        issuer_cik,
                        day.isoformat(),
                        f"edgar/data/{issuer_cik}/{accession_two}.txt",
                    ),
                )
            ),
            accession_one: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        shares=(100,),
                        issuer_cik=issuer_cik,
                        owner_cik=filer_cik,
                    )
                )
            ),
            accession_two: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        shares=(200,),
                        issuer_cik=issuer_cik,
                        owner_cik=filer_cik,
                    )
                )
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
async def test_ambiguous_amendment_marks_amendment_and_originals_ineligible(
    engine: None,
) -> None:
    day_one = date(2026, 7, 10)
    day_two = date(2026, 7, 13)
    issuer_cik = "1045810"
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
                        "NVIDIA CORP",
                        issuer_cik,
                        day_one.isoformat(),
                        f"edgar/data/{issuer_cik}/{original_one}.txt",
                    ),
                    (
                        "4",
                        "NVIDIA CORP",
                        issuer_cik,
                        day_one.isoformat(),
                        f"edgar/data/{issuer_cik}/{original_two}.txt",
                    ),
                )
            ),
            "form.20260713.idx": _FakeResp(
                text=_daily_index(
                    (
                        "4/A",
                        "NVIDIA CORP",
                        issuer_cik,
                        day_two.isoformat(),
                        f"edgar/data/{issuer_cik}/{amendment}.txt",
                    )
                )
            ),
            original_one: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        shares=(100,),
                        issuer_cik=issuer_cik,
                        owner_cik=filer_cik,
                    )
                )
            ),
            original_two: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        shares=(200,),
                        issuer_cik=issuer_cik,
                        owner_cik=filer_cik,
                    )
                )
            ),
            amendment: _FakeResp(
                text=_full_submission(
                    _form4_xml(
                        document_type="4/A",
                        shares=(300,),
                        issuer_cik=issuer_cik,
                        owner_cik=filer_cik,
                        date_of_original_submission=day_one.isoformat(),
                    )
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

    assert {row["accession"] for row in rows} == {
        original_one,
        original_two,
        amendment,
    }
    assert all(row["cluster_eligible"] is False for row in rows)
    assert all(row["amendment_match_status"] == "ambiguous" for row in rows)
    assert all(
        row["amendment_ambiguity_evidence"]
        == [original_one, original_two]
        for row in rows
    )


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


@pytest.mark.parametrize("original_date", ["", "not-a-date"])
def test_unusable_amendment_date_taints_same_owner_original_only(
    original_date: str,
) -> None:
    issuer_cik = "0001045810"

    def filing(
        accession: str,
        *,
        owner_cik: str,
        is_amendment: bool,
        filed_at: str,
    ) -> dict[str, Any]:
        row = sec._parse_form4_xml(
            _form4_xml(
                document_type="4/A" if is_amendment else "4",
                issuer_cik=issuer_cik,
                owner_cik=owner_cik,
                date_of_original_submission=(
                    original_date if is_amendment else ""
                ),
            ),
            accession=accession,
        )[0]
        filing_url = f"https://www.sec.gov/Archives/{accession}.txt"
        row.update({"filed_at": filed_at, "filing_url": filing_url})
        return {
            "accession": accession,
            "filed_at": filed_at,
            "filing_url": filing_url,
            "is_amendment": is_amendment,
            "issuer_cik": issuer_cik,
            "filer_cik": row["filer_cik"],
            "filer_name": row["filer_name"],
            "reporting_owners": row["reporting_owners"],
            "date_of_original_submission": (
                original_date if is_amendment else ""
            ),
            "rows": [row],
        }

    affected = filing(
        "0001234567-26-000501",
        owner_cik="0001234567",
        is_amendment=False,
        filed_at="2026-07-10",
    )
    unrelated = filing(
        "0007654321-26-000502",
        owner_cik="0007654321",
        is_amendment=False,
        filed_at="2026-07-10",
    )
    amendment = filing(
        "0001234567-26-000503",
        owner_cik="0001234567",
        is_amendment=True,
        filed_at="2026-07-13",
    )

    rows = sec._select_filing_versions([affected, unrelated, amendment])
    by_accession = {row["accession"]: row for row in rows}

    for accession in (affected["accession"], amendment["accession"]):
        assert by_accession[accession]["cluster_eligible"] is False
        assert by_accession[accession]["amendment_match_status"] == (
            "ambiguous"
        )
        assert by_accession[accession][
            "amendment_ambiguity_evidence"
        ] == [affected["accession"]]
    assert by_accession[unrelated["accession"]]["cluster_eligible"] is True
    assert by_accession[unrelated["accession"]][
        "amendment_match_status"
    ] == "not_amendment"


@pytest.mark.parametrize(
    ("amendment_owners", "original_owner_sets", "affected_accessions"),
    [
        (
            ("A",),
            (("A", "B"), ("C",)),
            ["original-1"],
        ),
        (
            ("A", "B"),
            (("A",), ("B",), ("C",)),
            ["original-1", "original-2"],
        ),
    ],
)
def test_ambiguous_amendment_taints_any_overlapping_owner_originals(
    amendment_owners: tuple[str, ...],
    original_owner_sets: tuple[tuple[str, ...], ...],
    affected_accessions: list[str],
) -> None:
    issuer_cik = "0001045810"

    def filing(
        accession: str,
        *,
        owners: tuple[str, ...],
        is_amendment: bool,
    ) -> dict[str, Any]:
        owner_records = [
            {
                "filer_cik": f"{index + 1:010d}",
                "filer_name": owner,
                "role": "director",
            }
            for index, owner in enumerate(("A", "B", "C"))
            if owner in owners
        ]
        row = sec._parse_form4_xml(
            _form4_xml(
                document_type="4/A" if is_amendment else "4",
                issuer_cik=issuer_cik,
                owner_cik=owner_records[0]["filer_cik"],
                owner_name=owner_records[0]["filer_name"],
                date_of_original_submission=(
                    "2026-07-10" if is_amendment else ""
                ),
            ),
            accession=accession,
        )[0]
        row["reporting_owners"] = owner_records
        filing_url = f"https://www.sec.gov/Archives/{accession}.txt"
        row.update(
            {
                "filed_at": "2026-07-13" if is_amendment else "2026-07-10",
                "filing_url": filing_url,
            }
        )
        return {
            "accession": accession,
            "filed_at": row["filed_at"],
            "filing_url": filing_url,
            "is_amendment": is_amendment,
            "issuer_cik": issuer_cik,
            "filer_cik": row["filer_cik"],
            "filer_name": row["filer_name"],
            "reporting_owners": owner_records,
            "date_of_original_submission": (
                "2026-07-10" if is_amendment else ""
            ),
            "rows": [row],
        }

    originals = [
        filing(
            f"original-{index}",
            owners=owners,
            is_amendment=False,
        )
        for index, owners in enumerate(original_owner_sets, start=1)
    ]
    amendment = filing(
        "amendment-1",
        owners=amendment_owners,
        is_amendment=True,
    )

    rows = sec._select_filing_versions([*originals, amendment])
    by_accession = {row["accession"]: row for row in rows}

    assert by_accession["amendment-1"]["amendment_match_status"] == "ambiguous"
    assert (
        by_accession["amendment-1"]["amendment_ambiguity_evidence"]
        == affected_accessions
    )
    for accession in affected_accessions:
        assert by_accession[accession]["cluster_eligible"] is False
        assert by_accession[accession]["amendment_match_status"] == "ambiguous"
        assert (
            by_accession[accession]["amendment_ambiguity_evidence"]
            == affected_accessions
        )
    unrelated = {
        original["accession"]
        for original in originals
        if original["accession"] not in affected_accessions
    }
    assert unrelated
    assert all(by_accession[accession]["cluster_eligible"] for accession in unrelated)


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

    with pytest.raises(ValueError, match="max_concurrent_filing_fetches"):
        sec.SecForm4Adapter(
            http_client=http,
            max_concurrent_filing_fetches=0,
        )
