"""Stream B insider-cluster classification and fetch tests."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.sec_form4_adapter import (
    _parse_form4_xml,
    _select_filing_versions,
)
from argosy.services.signal_streams.insider import (
    InsiderClusterConfig,
    InsiderClusterStream,
    InsiderMarketSnapshot,
    _deduplicate_rows,
    _transaction_identity,
    classify_insider_transactions,
    cluster_strength,
    market_cap_floor,
)


def _snapshot(
    *,
    price: float | None = 25.0,
    market_cap: float | None = 100_000_000,
    average_volume: float | None = 2_000_000,
    ticker: str = "ACME",
) -> InsiderMarketSnapshot:
    return InsiderMarketSnapshot(
        price=price,
        market_cap=market_cap,
        average_volume=average_volume,
        quote_source_url=f"https://finance.yahoo.com/quote/{ticker}",
    )


def _row(
    *,
    ticker: str = "ACME",
    accession: str = "0000000001-26-000001",
    transaction_index: int = 0,
    filer_cik: str = "0000001001",
    filer_name: str = "Alice Buyer",
    role: str = "director",
    transaction_date: str = "2026-07-01",
    filed_at: str = "2026-07-02",
    transaction_code: str = "P",
    acquired_disposed_code: str | None = "A",
    shares: float | None = 600,
    price_per_share: float | None = 100,
    value_usd: float | None = 60_000,
    post_transaction_holdings: float | None = 10_000,
    is_derivative: bool = False,
    is_10b5_1: bool | None = False,
    cluster_eligible: bool = True,
    is_amendment: bool = False,
    filing_identity: str | None = None,
    security_title: str = "Common Stock",
    direct_or_indirect_ownership: str = "D",
    nature_of_ownership: str = "",
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "accession": accession,
        "filing_identity": filing_identity or accession,
        "transaction_index": transaction_index,
        "filer_cik": filer_cik,
        "filer_name": filer_name,
        "role": role,
        "transaction_date": transaction_date,
        "filed_at": filed_at,
        "transaction_code": transaction_code,
        "acquired_disposed_code": acquired_disposed_code,
        "shares": shares,
        "price_per_share": price_per_share,
        "value_usd": value_usd,
        "post_transaction_holdings": post_transaction_holdings,
        "security_title": security_title,
        "direct_or_indirect_ownership": direct_or_indirect_ownership,
        "nature_of_ownership": nature_of_ownership,
        "is_derivative": is_derivative,
        "is_10b5_1": is_10b5_1,
        "tenb5_1_evidence": [],
        "document_has_10b5_1": False,
        "document_10b5_1_evidence": [],
        "cluster_eligible": cluster_eligible,
        "is_amendment": is_amendment,
        "document_type": "4/A" if is_amendment else "4",
        "amendment_match_status": "matched" if is_amendment else "not_amendment",
        "amendment_ambiguity_evidence": [],
        "source_urls": [f"https://www.sec.gov/Archives/{accession}.txt"],
    }


def _second_buyer(**overrides: Any) -> dict[str, Any]:
    values = {
        "accession": "0000000002-26-000002",
        "filer_cik": "0000001002",
        "filer_name": "Bob Officer",
        "role": "officer (Chief Technology Officer)",
        "transaction_date": "2026-07-04",
        "filed_at": "2026-07-06",
        "shares": 400,
        "price_per_share": 100,
        "value_usd": 40_000,
    }
    values.update(overrides)
    return _row(**values)


def _classify(
    rows: list[dict[str, Any]],
    *,
    snapshots: dict[str, InsiderMarketSnapshot] | None = None,
    config: InsiderClusterConfig | None = None,
    through: date = date(2026, 7, 10),
):
    return classify_insider_transactions(
        rows,
        snapshots=snapshots or {"ACME": _snapshot()},
        config=config or InsiderClusterConfig(),
        through=through,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lookback_days", 0),
        ("recent_scan_days", 0),
        ("recent_scan_days", 15),
        ("min_distinct_buyers", 1),
        ("min_cluster_value_usd", 0),
        ("min_cluster_value_market_cap_bps", -0.1),
        ("min_distinct_sellers", 1),
        ("min_stake_sale_pct", 0),
        ("min_stake_sale_pct", 100),
    ],
)
def test_insider_config_validates_every_threshold(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        InsiderClusterConfig(**{field: value})


def test_exact_buy_thresholds_nominate_officer_and_director_with_full_evidence() -> None:
    nominations = _classify([_row(), _second_buyer()])

    assert len(nominations) == 1
    nomination = nominations[0]
    assert nomination.ticker == "ACME"
    assert nomination.direction == "long"
    assert nomination.route_to_funnel is True
    assert nomination.as_of == date(2026, 7, 6)
    assert 0 < nomination.strength < 1
    assert nomination.evidence["distinct_insider_count"] == 2
    assert nomination.evidence["aggregate_value_usd"] == 100_000
    assert nomination.evidence["market_cap"] == 100_000_000
    assert nomination.evidence["threshold"]["effective_floor_usd"] == 100_000
    assert nomination.evidence["max_filing_lag_days"] == 2
    assert nomination.evidence["price"] == 25.0
    assert nomination.evidence["average_volume"] == 2_000_000
    assert len(nomination.evidence["transactions"]) == 2
    for transaction in nomination.evidence["transactions"]:
        assert {
            "accession",
            "transaction_index",
            "filer_cik",
            "filer_name",
            "role",
            "transaction_date",
            "filed_at",
            "value_usd",
            "is_10b5_1",
            "tenb5_1_evidence",
            "is_amendment",
            "amendment_match_status",
            "source_urls",
            "filing_lag_days",
        } <= transaction.keys()


def test_one_distinct_insider_is_rejected_even_across_multiple_rows() -> None:
    rows = [
        _row(),
        _second_buyer(filer_cik="0000001001", filer_name="Alice Buyer"),
    ]
    assert _classify(rows) == []


@pytest.mark.parametrize("status", [True, None])
def test_10b5_true_or_unknown_is_excluded(status: bool | None) -> None:
    rows = [_row(is_10b5_1=status), _second_buyer()]
    assert _classify(rows) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"transaction_code": "A"},
        {"transaction_code": "M"},
        {"is_derivative": True},
        {"acquired_disposed_code": "D"},
    ],
)
def test_grants_options_derivatives_and_dispositions_are_excluded(
    overrides: dict[str, Any],
) -> None:
    assert _classify([_row(**overrides), _second_buyer()]) == []


def test_absent_acquired_code_is_accepted_for_purchase() -> None:
    assert len(_classify([_row(acquired_disposed_code=None), _second_buyer()])) == 1


def test_market_cap_scaled_floor_controls_nomination() -> None:
    config = InsiderClusterConfig(min_cluster_value_market_cap_bps=0.5)
    snapshot = _snapshot(market_cap=4_000_000_000)
    assert market_cap_floor(4_000_000_000, config=config) == 200_000
    below = [_row(value_usd=100_000), _second_buyer(value_usd=99_999)]
    exact = [_row(value_usd=100_000), _second_buyer(value_usd=100_000)]

    assert _classify(below, snapshots={"ACME": snapshot}, config=config) == []
    result = _classify(exact, snapshots={"ACME": snapshot}, config=config)
    assert result[0].evidence["threshold"]["market_cap_scaled_usd"] == 200_000


@pytest.mark.parametrize(
    "rows,snapshot",
    [
        ([_row(shares=None), _second_buyer()], _snapshot()),
        ([_row(price_per_share=None), _second_buyer()], _snapshot()),
        ([_row(value_usd=None), _second_buyer()], _snapshot()),
        ([_row(), _second_buyer()], _snapshot(market_cap=None)),
        ([_row(), _second_buyer()], _snapshot(price=None)),
    ],
)
def test_missing_transaction_or_market_data_prevents_nomination(
    rows: list[dict[str, Any]], snapshot: InsiderMarketSnapshot
) -> None:
    assert _classify(rows, snapshots={"ACME": snapshot}) == []


def test_dedup_hash_and_evidence_order_are_stable_across_input_order() -> None:
    rows = [_row(), _second_buyer()]
    forward = _classify(rows)[0]
    reverse = _classify(list(reversed(rows)))[0]

    assert forward.dedup_key == reverse.dedup_key
    assert forward.evidence["transactions"] == reverse.evidence["transactions"]


def test_transaction_identity_separates_derivative_and_non_derivative_tables() -> None:
    non_derivative = _row()
    derivative = _row(is_derivative=True)

    assert _transaction_identity(non_derivative) != _transaction_identity(derivative)
    assert len(_deduplicate_rows([derivative, non_derivative])) == 2

    rows = [derivative, non_derivative, _second_buyer()]
    forward = _classify(rows)[0]
    reverse = _classify(list(reversed(rows)))[0]
    assert forward.dedup_key == reverse.dedup_key
    assert len(forward.evidence["transactions"]) == 2
    assert all(
        transaction["is_derivative"] is False for transaction in forward.evidence["transactions"]
    )


def _filing(
    row: dict[str, Any],
    *,
    filed_at: str,
    is_amendment: bool,
    date_of_original_submission: str = "",
) -> dict[str, Any]:
    accession = str(row["accession"])
    filing_url = f"https://www.sec.gov/Archives/{accession}.txt"
    row.update(
        {
            "filed_at": filed_at,
            "filing_url": filing_url,
            "is_amendment": is_amendment,
            "date_of_original_submission": date_of_original_submission,
        }
    )
    return {
        "accession": accession,
        "filed_at": filed_at,
        "filing_url": filing_url,
        "is_amendment": is_amendment,
        "issuer_cik": "0000009999",
        "filer_cik": row["filer_cik"],
        "filer_name": row["filer_name"],
        "date_of_original_submission": date_of_original_submission,
        "rows": [row],
    }


def test_matched_amendment_preserves_cluster_dedup_identity() -> None:
    original_a = _row(
        accession="0000000001-26-000100",
        filer_cik="0000001001",
        filer_name="Alice Buyer",
        transaction_date="2026-07-01",
        filed_at="2026-07-02",
    )
    original_b = _second_buyer(accession="0000000002-26-000200")
    original_filings = [
        _filing(original_a, filed_at="2026-07-02", is_amendment=False),
        _filing(original_b, filed_at="2026-07-06", is_amendment=False),
    ]
    original_rows = _select_filing_versions(original_filings)
    original_nomination = _classify(original_rows)[0]

    amended_a = _row(
        accession="0000000001-26-000101",
        filer_cik="0000001001",
        filer_name="Alice Buyer",
        transaction_date="2026-07-01",
        filed_at="2026-07-07",
        shares=650,
        value_usd=65_000,
        is_amendment=True,
    )
    amended_filings = [
        original_filings[1],
        _filing(
            amended_a,
            filed_at="2026-07-07",
            is_amendment=True,
            date_of_original_submission="2026-07-02",
        ),
        original_filings[0],
    ]
    amended_rows = _select_filing_versions(amended_filings)
    amended_nomination = _classify(list(reversed(amended_rows)))[0]

    assert amended_nomination.dedup_key == original_nomination.dedup_key
    amended_transaction = next(
        transaction
        for transaction in amended_nomination.evidence["transactions"]
        if transaction["filer_cik"] == "0000001001"
    )
    assert amended_transaction["accession"] == "0000000001-26-000101"
    assert amended_transaction["filing_identity"] == "0000000001-26-000100"
    assert len(amended_transaction["source_urls"]) == 2


def test_strength_is_monotonic_in_count_and_value_with_threshold_headroom() -> None:
    config = InsiderClusterConfig()
    exact = cluster_strength(
        distinct_count=2, aggregate_value_usd=100_000, floor_usd=100_000, config=config
    )
    more_people = cluster_strength(
        distinct_count=3, aggregate_value_usd=100_000, floor_usd=100_000, config=config
    )
    more_value = cluster_strength(
        distinct_count=2, aggregate_value_usd=200_000, floor_usd=100_000, config=config
    )
    strongest = cluster_strength(
        distinct_count=4, aggregate_value_usd=300_000, floor_usd=100_000, config=config
    )

    assert 0 < exact < more_people < strongest < 1
    assert exact < more_value < strongest


def test_amendment_ineligible_row_is_excluded() -> None:
    rows = [
        _row(
            is_amendment=True,
            cluster_eligible=False,
        ),
        _second_buyer(),
    ]
    assert _classify(rows) == []


def _seller(
    *,
    filer: int,
    role: str,
    shares: float,
    post_holdings: float,
    accession: str,
) -> dict[str, Any]:
    return _row(
        accession=accession,
        filer_cik=str(filer),
        filer_name=f"Seller {filer}",
        role=role,
        transaction_code="S",
        acquired_disposed_code="D",
        shares=shares,
        price_per_share=10,
        value_usd=shares * 10,
        post_transaction_holdings=post_holdings,
    )


def test_exact_twenty_percent_sale_is_rejected_but_above_twenty_warns() -> None:
    exact = _seller(
        filer=1,
        role="officer (CEO)",
        shares=200,
        post_holdings=800,
        accession="sale-1",
    )
    above = _seller(
        filer=2,
        role="officer (Chief Financial Officer)",
        shares=201,
        post_holdings=799,
        accession="sale-2",
    )
    above["filed_at"] = "2026-07-05"
    assert _classify([exact, above]) == []

    first_above = {**exact, "shares": 201, "post_transaction_holdings": 799}
    warning = _classify([first_above, above])[0]
    assert warning.direction == "short"
    assert warning.route_to_funnel is False
    assert warning.as_of == date(2026, 7, 5)
    assert warning.evidence["warning_only"] is True
    assert warning.evidence["distinct_insider_count"] == 2
    assert all(
        transaction["stake_sale_pct"] > 20 for transaction in warning.evidence["transactions"]
    )


def test_generic_officer_is_not_c_suite_and_two_c_suite_sellers_are_required() -> None:
    ceo = _seller(
        filer=1,
        role="officer (CEO)",
        shares=300,
        post_holdings=700,
        accession="sale-1",
    )
    generic = _seller(
        filer=2,
        role="officer",
        shares=300,
        post_holdings=700,
        accession="sale-2",
    )
    director = _seller(
        filer=3,
        role="director",
        shares=300,
        post_holdings=700,
        accession="sale-3",
    )
    assert _classify([ceo, generic, director]) == []


@pytest.mark.parametrize(
    "role",
    [
        "officer (Vice President)",
        "officer (Senior Vice President)",
        "officer (Executive Vice President)",
        "officer (Assistant to the President)",
        "officer (Division President)",
    ],
)
def test_vice_president_titles_are_not_c_suite(role: str) -> None:
    ceo = _seller(
        filer=1,
        role="officer (CEO)",
        shares=300,
        post_holdings=700,
        accession="sale-1",
    )
    vice_president = _seller(
        filer=2,
        role=role,
        shares=300,
        post_holdings=700,
        accession="sale-2",
    )
    assert _classify([ceo, vice_president]) == []


@pytest.mark.parametrize(
    "role",
    [
        "officer (CTO)",
        "officer (CIO)",
        "officer (CMO)",
        "officer (CRO)",
        "officer (Chief Technology Officer)",
        "officer (Chief Investment Officer)",
        "officer (Chief Marketing Officer)",
        "officer (Chief Risk Officer)",
        "officer (President)",
        "officer (President & CEO)",
        "officer (Executive Vice President and CFO)",
    ],
)
def test_supported_c_suite_titles_qualify(role: str) -> None:
    ceo = _seller(
        filer=1,
        role="officer (CEO)",
        shares=300,
        post_holdings=700,
        accession="sale-1",
    )
    executive = _seller(
        filer=2,
        role=role,
        shares=300,
        post_holdings=700,
        accession="sale-2",
    )
    warning = _classify([ceo, executive])[0]
    assert warning.direction == "short"
    assert warning.evidence["distinct_insider_count"] == 2


def test_split_sales_qualify_on_per_holder_aggregate_stake() -> None:
    rows = [
        _seller(
            filer=1,
            role="officer (CEO)",
            shares=110,
            post_holdings=890,
            accession="sale-1",
        ),
        {
            **_seller(
                filer=1,
                role="officer (CEO)",
                shares=110,
                post_holdings=780,
                accession="sale-1",
            ),
            "transaction_index": 1,
        },
        _seller(
            filer=2,
            role="officer (CFO)",
            shares=110,
            post_holdings=890,
            accession="sale-2",
        ),
        {
            **_seller(
                filer=2,
                role="officer (CFO)",
                shares=110,
                post_holdings=780,
                accession="sale-2",
            ),
            "transaction_index": 1,
        },
    ]

    warning = _classify(rows)[0]
    assert warning.direction == "short"
    assert [seller["stake_sale_pct"] for seller in warning.evidence["seller_aggregates"]] == [
        22.0,
        22.0,
    ]
    assert all(
        transaction["holder_aggregate_stake_sale_pct"] == 22.0
        for transaction in warning.evidence["transactions"]
    )
    assert all(
        pool["ownership_pool"]
        == {
            "security_title": "common stock",
            "direct_or_indirect_ownership": "D",
            "nature_of_ownership": "",
            "transaction_table": "non_derivative",
        }
        for pool in warning.evidence["seller_pools"]
    )


def test_direct_and_trust_sales_never_combine_into_false_threshold() -> None:
    rows: list[dict[str, Any]] = []
    for filer, role, accession in [
        (1, "officer (CEO)", "sale-1"),
        (2, "officer (CFO)", "sale-2"),
    ]:
        rows.extend(
            [
                _seller(
                    filer=filer,
                    role=role,
                    shares=120,
                    post_holdings=880,
                    accession=accession,
                ),
                {
                    **_seller(
                        filer=filer,
                        role=role,
                        shares=120,
                        post_holdings=880,
                        accession=accession,
                    ),
                    "transaction_index": 1,
                    "direct_or_indirect_ownership": "I",
                    "nature_of_ownership": "Family Trust",
                },
            ]
        )

    assert _classify(rows) == []


def test_exact_twenty_percent_aggregate_sale_is_rejected() -> None:
    rows = [
        _seller(
            filer=1,
            role="officer (CEO)",
            shares=100,
            post_holdings=900,
            accession="sale-1",
        ),
        {
            **_seller(
                filer=1,
                role="officer (CEO)",
                shares=100,
                post_holdings=800,
                accession="sale-1",
            ),
            "transaction_index": 1,
        },
        _seller(
            filer=2,
            role="officer (CFO)",
            shares=300,
            post_holdings=700,
            accession="sale-2",
        ),
    ]
    assert _classify(rows) == []


@pytest.mark.parametrize("missing_field", ["post_transaction_holdings", "value_usd"])
def test_any_unverifiable_split_sale_disqualifies_the_holder(missing_field: str) -> None:
    first = _seller(
        filer=1,
        role="officer (CEO)",
        shares=250,
        post_holdings=750,
        accession="sale-1",
    )
    second = {
        **_seller(
            filer=1,
            role="officer (CEO)",
            shares=10,
            post_holdings=740,
            accession="sale-1",
        ),
        "transaction_index": 1,
        missing_field: None,
    }
    other = _seller(
        filer=2,
        role="officer (CFO)",
        shares=300,
        post_holdings=700,
        accession="sale-2",
    )
    assert _classify([first, second, other]) == []


def test_unrelated_unverifiable_ticker_does_not_blank_valid_cluster() -> None:
    rows = [
        _row(),
        _second_buyer(),
        _row(ticker="BAD", accession="bad-1", filer_cik="9001"),
        _second_buyer(ticker="BAD", accession="bad-2", filer_cik="9002"),
    ]
    nominations = _classify(rows, snapshots={"ACME": _snapshot()})
    assert [nomination.ticker for nomination in nominations] == ["ACME"]


def test_missing_market_cap_keeps_sell_warning_but_omits_buy_cluster() -> None:
    rows = [
        _row(),
        _second_buyer(),
        _seller(
            filer=3,
            role="officer (CEO)",
            shares=300,
            post_holdings=700,
            accession="sale-3",
        ),
        _seller(
            filer=4,
            role="officer (CFO)",
            shares=300,
            post_holdings=700,
            accession="sale-4",
        ),
    ]

    nominations = _classify(
        rows,
        snapshots={"ACME": _snapshot(market_cap=None)},
    )
    assert [(nomination.direction, nomination.route_to_funnel) for nomination in nominations] == [
        ("short", False)
    ]
    assert (
        _classify(
            rows,
            snapshots={"ACME": _snapshot(price=None, market_cap=None)},
        )
        == []
    )


class _FixtureSecAdapter:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[date, date | None]] = []

    async def get_form4_for_date_range(
        self, start_date: date, through: date | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((start_date, through))
        return list(self.rows)


def _official_schw_rows() -> list[dict[str, Any]]:
    fixtures = [
        (
            "0001062993-23-006879",
            "sec_form4_schw_bettinger_20230314.xml",
        ),
        (
            "0001062993-23-006880",
            "sec_form4_schw_crawford_20230314.xml",
        ),
    ]
    rows: list[dict[str, Any]] = []
    for accession, filename in fixtures:
        xml = (Path(__file__).parent / "fixtures" / filename).read_text(encoding="utf-8")
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/316709/{accession.replace('-', '')}/form4.xml"
        )
        parsed = _parse_form4_xml(xml, accession=accession)
        for row in parsed:
            row.update(
                {
                    "filed_at": "2023-03-14",
                    "filing_url": filing_url,
                    "source_urls": [filing_url],
                    "cluster_eligible": True,
                    "filing_identity": accession,
                    "amendment_match_status": "not_amendment",
                    "amendment_ambiguity_evidence": [],
                }
            )
        rows.extend(parsed)
    return rows


def test_fetch_replays_official_schw_cluster_from_raw_xml() -> None:
    fixture = _official_schw_rows()
    assert [(row["shares"], row["price_per_share"]) for row in fixture] == [
        (50_000, 59.31),
        (5_000, 57.9561),
    ]
    assert {row["issuer_cik"] for row in fixture} == {"0000316709"}
    assert all(row["source_urls"][0].startswith("https://www.sec.gov/Archives/") for row in fixture)

    sec = _FixtureSecAdapter(fixture)
    config = InsiderClusterConfig(min_cluster_value_market_cap_bps=0.1)
    stream = InsiderClusterStream(
        config=config,
        sec_adapter=sec,
        market_snapshot=lambda ticker: _snapshot(
            ticker=ticker,
            price=58.0,
            market_cap=100_000_000_000,
            average_volume=20_000_000,
        ),
        today=lambda: date(2023, 3, 15),
    )
    nominations = asyncio.run(asyncio.to_thread(stream.fetch, None, since=date(2023, 3, 14)))

    assert [nomination.ticker for nomination in nominations] == ["SCHW"]
    nomination = nominations[0]
    assert nomination.as_of == date(2023, 3, 14)
    assert nomination.evidence["aggregate_value_usd"] == pytest.approx(3_255_280.5)
    assert nomination.evidence["threshold"]["market_cap_bps"] == 0.1
    assert {transaction["accession"] for transaction in nomination.evidence["transactions"]} == {
        "0001062993-23-006879",
        "0001062993-23-006880",
    }
    assert sec.calls == [(date(2023, 2, 28), date(2023, 3, 14))]


def test_sec_adapter_outage_fails_the_stream() -> None:
    class FailingSecAdapter:
        async def get_form4_for_date_range(self, start_date, through=None):
            raise MissingDataSourceError("SEC unavailable")

    stream = InsiderClusterStream(
        sec_adapter=FailingSecAdapter(),
        market_snapshot=lambda ticker: _snapshot(ticker=ticker),
        today=lambda: date(2026, 7, 11),
    )
    with pytest.raises(MissingDataSourceError, match="SEC unavailable"):
        stream.fetch(None, since=date(2026, 7, 9))
