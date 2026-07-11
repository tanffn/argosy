"""Stream B insider-cluster classification and fetch tests."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.sec_form4_adapter import (
    MAX_GLOBAL_DATE_RANGE_DAYS,
    _parse_form4_xml,
    _select_filing_versions,
)
from argosy.config import InsiderClusterSignalConfig
from argosy.orchestrator.loops.signal_streams_daily import _default_streams
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
    issuer_cik: str = "0000009999",
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
        "issuer_cik": issuer_cik,
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
    availability_since: date | None = None,
):
    return classify_insider_transactions(
        rows,
        snapshots=snapshots or {"ACME": _snapshot()},
        config=config or InsiderClusterConfig(),
        through=through,
        availability_since=availability_since,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lookback_days", 0),
        ("recent_scan_days", 0),
        ("recent_scan_days", 15),
        ("index_publication_lag_days", 0),
        ("min_distinct_buyers", 1),
        ("min_cluster_value_usd", 0),
        ("min_cluster_value_market_cap_bps", -0.1),
        ("min_distinct_sellers", 1),
        ("min_stake_sale_pct", 0),
        ("min_stake_sale_pct", 100),
        ("warning_ttl_days", 0),
        ("warning_ttl_days", 366),
        ("cursor_max_catchup_days", 0),
        ("cursor_max_catchup_days", 32),
    ],
)
def test_insider_config_validates_every_threshold(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        InsiderClusterConfig(**{field: value})


def test_insider_publication_lag_config_defaults_and_validates() -> None:
    assert InsiderClusterConfig().index_publication_lag_days == 2
    assert InsiderClusterSignalConfig().index_publication_lag_days == 2

    with pytest.raises(ValueError):
        InsiderClusterSignalConfig(index_publication_lag_days=0)


def test_default_streams_passes_configured_publication_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insider = SimpleNamespace(
        enabled=True,
        lookback_days=14,
        recent_scan_days=2,
        index_publication_lag_days=3,
        daily_pull_days=1,
        ledger_horizon_days=45,
        min_distinct_buyers=2,
        min_cluster_value_usd=100_000,
        min_cluster_value_market_cap_bps=0.5,
        min_distinct_sellers=2,
        min_stake_sale_pct=20,
        warning_ttl_days=30,
        cursor_max_catchup_days=31,
    )
    monkeypatch.setattr(
        "argosy.config.load_signal_streams_config",
        lambda _user_id: SimpleNamespace(
            enabled=True,
            gov_contracts=SimpleNamespace(enabled=False),
            insider_cluster=insider,
        ),
    )

    streams = _default_streams("ariel")

    assert len(streams) == 1
    assert streams[0].config.index_publication_lag_days == 3
    assert streams[0].config.daily_pull_days == 1
    assert streams[0].config.ledger_horizon_days == 45
    assert streams[0].user_id == "ariel"


def test_example_config_documents_daily_pull_boundary() -> None:
    path = Path(__file__).parents[1] / "configs" / "example" / "agent_settings.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert (
        payload["signal_streams"]["insider_cluster"][
            "index_publication_lag_days"
        ]
        == 2
    )
    assert (
        payload["signal_streams"]["insider_cluster"]["daily_pull_days"]
        == 1
    )


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


def test_cluster_filed_before_availability_is_not_reemitted() -> None:
    assert (
        _classify(
            [_row(), _second_buyer()],
            availability_since=date(2026, 7, 7),
        )
        == []
    )


def test_cluster_transactions_must_share_one_fourteen_day_window() -> None:
    rows = [
        _row(
            transaction_date="2026-07-01",
            filed_at="2026-07-16",
        ),
        _second_buyer(
            transaction_date="2026-07-15",
            filed_at="2026-07-17",
        ),
    ]
    assert (
        _classify(
            rows,
            through=date(2026, 7, 20),
            availability_since=date(2026, 7, 1),
        )
        == []
    )


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


def _two_disjoint_buy_clusters() -> list[dict[str, Any]]:
    return [
        _row(
            accession="buy-a1",
            filer_cik="1001",
            filer_name="Early Buyer A",
            transaction_date="2026-07-01",
            filed_at="2026-07-03",
        ),
        _second_buyer(
            accession="buy-a2",
            filer_cik="1002",
            filer_name="Early Buyer B",
            transaction_date="2026-07-02",
            filed_at="2026-07-04",
        ),
        _row(
            accession="buy-b1",
            filer_cik="2001",
            filer_name="Late Buyer A",
            transaction_date="2026-07-20",
            filed_at="2026-07-22",
        ),
        _second_buyer(
            accession="buy-b2",
            filer_cik="2002",
            filer_name="Late Buyer B",
            transaction_date="2026-07-21",
            filed_at="2026-07-23",
        ),
    ]


def test_disjoint_buy_clusters_emit_and_write_separate_dedup_events() -> None:
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.services.signal_streams.pipeline import process_nominations
    from argosy.state.models import Base, Prediction, User

    nominations = _classify(
        _two_disjoint_buy_clusters(),
        through=date(2026, 7, 30),
        availability_since=date(2026, 7, 1),
    )

    assert [(item.as_of, item.direction) for item in nominations] == [
        (date(2026, 7, 4), "long"),
        (date(2026, 7, 23), "long"),
    ]
    assert len({item.dedup_key for item in nominations}) == 2

    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id="ariel", plan="free"))
        session.commit()
        summary = process_nominations(
            session,
            user_id="ariel",
            nominations=nominations,
        )
        session.commit()
        rows = session.query(Prediction).all()
        assert summary.active + summary.quarantined == 2
        assert len(rows) == 4
        assert len({row.message_id.rsplit("|", 1)[0] for row in rows}) == 2
    engine.dispose()


def test_disjoint_clusters_from_different_rows_of_one_accession_have_unique_dedup() -> None:
    rows = [
        _row(
            accession="shared-accession",
            transaction_index=0,
            filer_cik="5101",
            transaction_date="2026-07-01",
            filed_at="2026-07-03",
        ),
        _second_buyer(
            accession="shared-accession",
            transaction_index=1,
            filer_cik="5102",
            transaction_date="2026-07-02",
            filed_at="2026-07-04",
        ),
        _row(
            accession="shared-accession",
            transaction_index=2,
            filer_cik="5201",
            transaction_date="2026-07-20",
            filed_at="2026-07-22",
        ),
        _second_buyer(
            accession="shared-accession",
            transaction_index=3,
            filer_cik="5202",
            transaction_date="2026-07-21",
            filed_at="2026-07-23",
        ),
    ]

    forward = _classify(
        rows,
        through=date(2026, 7, 30),
        availability_since=date(2026, 7, 1),
    )
    reverse = _classify(
        list(reversed(rows)),
        through=date(2026, 7, 30),
        availability_since=date(2026, 7, 1),
    )

    assert len(forward) == 2
    assert len({item.dedup_key for item in forward}) == 2
    assert [item.dedup_key for item in forward] == [
        item.dedup_key for item in reverse
    ]


def test_overlapping_buy_windows_collapse_to_one_maximal_nomination() -> None:
    rows = [
        _row(
            accession="overlap-1",
            filer_cik="3001",
            transaction_date="2026-07-01",
            filed_at="2026-07-03",
            value_usd=50_000,
        ),
        _second_buyer(
            accession="overlap-2",
            filer_cik="3002",
            transaction_date="2026-07-02",
            filed_at="2026-07-04",
            value_usd=50_000,
        ),
        _row(
            accession="overlap-3",
            filer_cik="3003",
            transaction_date="2026-07-03",
            filed_at="2026-07-05",
            value_usd=50_000,
        ),
    ]

    nominations = _classify(
        rows,
        through=date(2026, 7, 30),
        availability_since=date(2026, 7, 1),
    )

    assert len(nominations) == 1
    assert nominations[0].as_of == date(2026, 7, 5)
    assert len(nominations[0].evidence["transactions"]) == 3


def test_disjoint_cluster_output_order_is_stable_across_input_order() -> None:
    rows = _two_disjoint_buy_clusters()
    forward = _classify(
        rows,
        through=date(2026, 7, 30),
        availability_since=date(2026, 7, 1),
    )
    reverse = _classify(
        list(reversed(rows)),
        through=date(2026, 7, 30),
        availability_since=date(2026, 7, 1),
    )

    assert len(forward) == 2
    assert [(item.as_of, item.direction, item.dedup_key) for item in forward] == [
        (item.as_of, item.direction, item.dedup_key) for item in reverse
    ]


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


def test_disjoint_warning_clusters_are_preserved() -> None:
    early_a = _seller(
        filer=4101,
        role="officer (CEO)",
        shares=300,
        post_holdings=700,
        accession="warning-a1",
    )
    early_a.update(transaction_date="2026-07-01", filed_at="2026-07-03")
    early_b = _seller(
        filer=4102,
        role="officer (CFO)",
        shares=300,
        post_holdings=700,
        accession="warning-a2",
    )
    early_b.update(transaction_date="2026-07-02", filed_at="2026-07-04")
    late_a = _seller(
        filer=4201,
        role="officer (CEO)",
        shares=300,
        post_holdings=700,
        accession="warning-b1",
    )
    late_a.update(transaction_date="2026-07-20", filed_at="2026-07-22")
    late_b = _seller(
        filer=4202,
        role="officer (CFO)",
        shares=300,
        post_holdings=700,
        accession="warning-b2",
    )
    late_b.update(transaction_date="2026-07-21", filed_at="2026-07-23")

    nominations = _classify(
        [early_a, early_b, late_a, late_b],
        through=date(2026, 7, 30),
        availability_since=date(2026, 7, 1),
    )

    assert [(item.as_of, item.direction) for item in nominations] == [
        (date(2026, 7, 4), "short"),
        (date(2026, 7, 23), "short"),
    ]
    assert len({item.dedup_key for item in nominations}) == 2


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


def test_split_sales_on_different_days_never_combine_into_false_stake() -> None:
    rows: list[dict[str, Any]] = []
    for filer, role, accession in [
        (1, "officer (CEO)", "cross-day-1"),
        (2, "officer (CFO)", "cross-day-2"),
    ]:
        first = _seller(
            filer=filer,
            role=role,
            shares=110,
            post_holdings=890,
            accession=accession,
        )
        first["transaction_date"] = "2026-07-01"
        second = {
            **_seller(
                filer=filer,
                role=role,
                shares=110,
                post_holdings=780,
                accession=accession,
            ),
            "transaction_index": 1,
            "transaction_date": "2026-07-02",
        }
        rows.extend([first, second])

    assert _classify(rows) == []


def test_purchase_in_same_filing_date_pool_contaminates_split_sale_stake() -> None:
    rows: list[dict[str, Any]] = []
    for filer, role, accession in [
        (1, "officer (CEO)", "contaminated-1"),
        (2, "officer (CFO)", "contaminated-2"),
    ]:
        first_sale = _seller(
            filer=filer,
            role=role,
            shares=150,
            post_holdings=850,
            accession=accession,
        )
        purchase = {
            **_seller(
                filer=filer,
                role=role,
                shares=100,
                post_holdings=950,
                accession=accession,
            ),
            "transaction_index": 1,
            "transaction_code": "P",
            "acquired_disposed_code": "A",
        }
        second_sale = {
            **_seller(
                filer=filer,
                role=role,
                shares=100,
                post_holdings=850,
                accession=accession,
            ),
            "transaction_index": 2,
        }
        rows.extend([first_sale, purchase, second_sale])

    assert _classify(rows) == []


def test_ineligible_10b5_purchase_still_contaminates_same_block_split_sales() -> None:
    rows: list[dict[str, Any]] = []
    for filer, role, accession in [
        (1, "officer (CEO)", "planned-contamination-1"),
        (2, "officer (CFO)", "planned-contamination-2"),
    ]:
        first_sale = _seller(
            filer=filer,
            role=role,
            shares=150,
            post_holdings=850,
            accession=accession,
        )
        planned_purchase = {
            **_seller(
                filer=filer,
                role=role,
                shares=100,
                post_holdings=950,
                accession=accession,
            ),
            "transaction_index": 1,
            "transaction_code": "P",
            "acquired_disposed_code": "A",
            "is_10b5_1": True,
        }
        second_sale = {
            **_seller(
                filer=filer,
                role=role,
                shares=100,
                post_holdings=850,
                accession=accession,
            ),
            "transaction_index": 2,
        }
        rows.extend([first_sale, planned_purchase, second_sale])

    assert _classify(rows) == []


def test_ineligible_10b5_sells_contaminate_same_pool_stake_block() -> None:
    """Replay: planned sells must contaminate, not inflate stake to ~100%."""
    rows: list[dict[str, Any]] = []
    for filer, role, accession in [
        (1, "officer (CEO)", "planned-sell-1"),
        (2, "officer (CFO)", "planned-sell-2"),
    ]:
        planned_sale = {
            **_seller(
                filer=filer,
                role=role,
                shares=900,
                post_holdings=100,
                accession=accession,
            ),
            "is_10b5_1": True,
        }
        discretionary_sale = {
            **_seller(
                filer=filer,
                role=role,
                shares=100,
                post_holdings=0,
                accession=accession,
            ),
            "transaction_index": 1,
        }
        rows.extend([planned_sale, discretionary_sale])

    # Without contamination the discretionary 100-of-100 sale alone would
    # look like a 100% stake dump and fire a false C-suite warning.
    assert _classify(rows) == []


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


def _potential_prefilter_rows() -> list[dict[str, Any]]:
    rows = [
        _row(
            ticker=f"NOISE{i:03d}",
            accession=f"noise-{i}",
            filer_cik=f"8{i:04d}",
        )
        for i in range(200)
    ]
    rows.extend(
        [
            _row(
                ticker="GRANT",
                accession="grant-1",
                filer_cik="7001",
                transaction_code="A",
            ),
            _second_buyer(
                ticker="GRANT",
                accession="grant-2",
                filer_cik="7002",
                transaction_code="A",
            ),
            _row(
                ticker="PLAN",
                accession="plan-1",
                filer_cik="7101",
                is_10b5_1=True,
            ),
            _second_buyer(
                ticker="PLAN",
                accession="plan-2",
                filer_cik="7102",
                is_10b5_1=True,
            ),
            _row(
                ticker="BADV",
                accession="badv-1",
                filer_cik="7201",
                value_usd=None,
            ),
            _second_buyer(
                ticker="BADV",
                accession="badv-2",
                filer_cik="7202",
                value_usd=None,
            ),
            _row(
                ticker="UUUU",
                accession="uuuu-1",
                filer_cik="7301",
            ),
            _second_buyer(
                ticker="UUUU",
                accession="uuuu-2",
                filer_cik="7302",
            ),
            _row(
                ticker="WRAP",
                accession="wrap-1",
                filer_cik="7401",
            ),
            _second_buyer(
                ticker="WRAP",
                accession="wrap-2",
                filer_cik="7402",
            ),
            _row(
                ticker="PALI",
                accession="pali-1",
                filer_cik="7501",
                acquired_disposed_code=None,
            ),
            _second_buyer(
                ticker="PALI",
                accession="pali-2",
                filer_cik="7502",
                acquired_disposed_code=None,
            ),
            _seller(
                filer=7601,
                role="officer (CEO)",
                shares=300,
                post_holdings=700,
                accession="sell-1",
            )
            | {"ticker": "SELL"},
            _seller(
                filer=7602,
                role="officer (CFO)",
                shares=300,
                post_holdings=700,
                accession="sell-2",
            )
            | {"ticker": "SELL"},
        ]
    )
    return rows


def test_prefilter_skips_noise_and_keeps_live_candidate_patterns() -> None:
    from argosy.services.signal_streams.insider import (
        potential_insider_nomination_tickers,
    )

    tickers = potential_insider_nomination_tickers(
        _potential_prefilter_rows(),
        config=InsiderClusterConfig(),
        through=date(2026, 7, 10),
        availability_since=date(2026, 6, 10),
    )

    assert tickers == ["PALI", "SELL", "UUUU", "WRAP"]


def test_prefilter_base_floor_never_applies_market_cap_scaled_floor() -> None:
    from argosy.services.signal_streams.insider import (
        potential_insider_nomination_tickers,
    )

    rows = [
        _row(ticker="BASE", accession="base-1", filer_cik="7311"),
        _second_buyer(
            ticker="BASE",
            accession="base-2",
            filer_cik="7312",
        ),
    ]
    tickers = potential_insider_nomination_tickers(
        rows,
        config=InsiderClusterConfig(
            min_cluster_value_market_cap_bps=1_000_000_000_000,
        ),
        through=date(2026, 7, 10),
        availability_since=date(2026, 6, 10),
    )

    assert tickers == ["BASE"]


def test_fetch_snapshots_only_potential_tickers_and_cap_can_still_reject() -> None:
    rows = _potential_prefilter_rows()
    rows.extend(
        [
            _row(
                ticker="CAP",
                accession="cap-1",
                filer_cik="7701",
            ),
            _second_buyer(
                ticker="CAP",
                accession="cap-2",
                filer_cik="7702",
            ),
        ]
    )
    sec = _FixtureSecAdapter(rows)
    snapshot_calls: list[str] = []

    def market_snapshot(ticker: str) -> InsiderMarketSnapshot:
        snapshot_calls.append(ticker)
        return _snapshot(
            ticker=ticker,
            market_cap=4_000_000_000 if ticker == "CAP" else 100_000_000,
        )

    stream = InsiderClusterStream(
        sec_adapter=sec,
        market_snapshot=market_snapshot,
        today=lambda: date(2026, 7, 12),
    )

    nominations = stream.fetch(None, since=date(2026, 6, 10))

    assert snapshot_calls == ["CAP", "PALI", "SELL", "UUUU", "WRAP"]
    assert "CAP" not in {nomination.ticker for nomination in nominations}
    assert {nomination.ticker for nomination in nominations} == {
        "PALI",
        "SELL",
        "UUUU",
        "WRAP",
    }


def test_one_potential_ticker_snapshot_failure_keeps_other_candidates() -> None:
    rows = [
        _row(ticker="BAD", accession="bad-1", filer_cik="7801"),
        _second_buyer(
            ticker="BAD",
            accession="bad-2",
            filer_cik="7802",
        ),
        _row(ticker="GOOD", accession="good-1", filer_cik="7901"),
        _second_buyer(
            ticker="GOOD",
            accession="good-2",
            filer_cik="7902",
        ),
    ]
    sec = _FixtureSecAdapter(rows)
    calls: list[str] = []

    def market_snapshot(ticker: str) -> InsiderMarketSnapshot:
        calls.append(ticker)
        if ticker == "BAD":
            raise TimeoutError("Yahoo timed out")
        return _snapshot(ticker=ticker)

    stream = InsiderClusterStream(
        sec_adapter=sec,
        market_snapshot=market_snapshot,
        today=lambda: date(2026, 7, 12),
    )

    nominations = stream.fetch(None, since=date(2026, 6, 10))

    assert calls == ["BAD", "GOOD"]
    assert [nomination.ticker for nomination in nominations] == ["GOOD"]


def test_stream_prepares_windows_once_and_reuses_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argosy.services.signal_streams.insider as insider_mod

    rows = _potential_prefilter_rows()
    config = InsiderClusterConfig()
    through = date(2026, 7, 10)
    availability_since = date(2026, 6, 10)
    snapshots = {
        ticker: _snapshot(ticker=ticker)
        for ticker in ("PALI", "SELL", "UUUU", "WRAP")
    }
    expected = classify_insider_transactions(
        rows,
        snapshots=snapshots,
        config=config,
        through=through,
        availability_since=availability_since,
    )
    prepare_calls = 0
    original_prepare = insider_mod.prepare_insider_windows

    def recording_prepare(*args, **kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(
        insider_mod,
        "prepare_insider_windows",
        recording_prepare,
    )
    sec = _FixtureSecAdapter(rows)
    market_calls: list[str] = []
    stream = InsiderClusterStream(
        config=config,
        sec_adapter=sec,
        market_snapshot=lambda ticker: (
            market_calls.append(ticker) or snapshots[ticker]
        ),
        today=lambda: date(2026, 7, 12),
    )

    actual = stream.fetch(None, since=availability_since)

    assert prepare_calls == 1
    assert market_calls == ["PALI", "SELL", "UUUU", "WRAP"]
    assert [
        (item.ticker, item.direction, item.as_of)
        for item in actual
    ] == [
        (item.ticker, item.direction, item.as_of)
        for item in expected
    ]


def test_dense_ticker_window_preparation_is_single_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argosy.services.signal_streams.insider as insider_mod

    start = date(2026, 7, 1)
    rows = [
        _row(
            ticker="DENSE",
            accession=f"dense-{index}",
            filer_cik=f"9{index:05d}",
            transaction_date=(
                start + timedelta(days=index % 30)
            ).isoformat(),
            filed_at=(
                start + timedelta(days=index % 30 + 1)
            ).isoformat(),
            value_usd=1_000,
        )
        for index in range(300)
    ]
    parse_calls = 0
    original_parse = insider_mod._parse_date

    def recording_parse(value):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(value)

    monkeypatch.setattr(insider_mod, "_parse_date", recording_parse)

    prepared = insider_mod.prepare_insider_windows(
        rows,
        config=InsiderClusterConfig(),
        through=date(2026, 8, 1),
        availability_since=date(2026, 7, 1),
    )

    assert list(prepared.windows_by_ticker) == ["DENSE"]
    assert len(prepared.windows_by_ticker["DENSE"]) == 30
    assert all(
        candidate.transaction_identities
        for candidate in prepared.windows_by_ticker["DENSE"]
    )
    assert prepared.availability_since == date(2026, 7, 1)
    assert prepared.through == date(2026, 8, 1)
    assert parse_calls <= len(rows) * 5


class _FixtureSecAdapter:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[date, date | None]] = []

    async def get_form4_for_date_range(
        self,
        start_date: date,
        through: date | None = None,
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


def test_joint_filing_is_one_reporting_group_not_two_independent_buyers() -> None:
    base = """<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-07-10</periodOfReport>
  <issuer>
    <issuerCik>0000009999</issuerCik>
    <issuerName>ACME CORP</issuerName>
    <issuerTradingSymbol>ACME</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0000001001</rptOwnerCik>
      <rptOwnerName>Alice Joint Buyer</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>0</isOfficer>
    </reportingOwnerRelationship>
  </reportingOwner>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0000001002</rptOwnerCik>
      <rptOwnerName>Bob Joint Buyer</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <officerTitle>Chief Technology Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-07-10</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>10.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>20000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""
    rows = _parse_form4_xml(base, accession="joint-1")
    rows[0].update(
        {
            "filed_at": "2026-07-10",
            "filing_identity": "joint-1",
            "cluster_eligible": True,
            "source_urls": ["https://www.sec.gov/Archives/joint-1.txt"],
            "amendment_match_status": "not_amendment",
            "amendment_ambiguity_evidence": [],
        }
    )

    nominations = _classify(rows)

    assert len(rows) == 1
    assert {
        owner["filer_cik"] for owner in rows[0]["reporting_owners"]
    } == {"0000001001", "0000001002"}
    assert nominations == []


def test_joint_c_suite_sale_fails_closed_without_owner_attribution() -> None:
    joint_sale = _seller(
        filer=9001,
        role="officer (CEO)",
        shares=300,
        post_holdings=700,
        accession="joint-sale",
    )
    joint_sale["reporting_owners"] = [
        {
            "filer_cik": "9001",
            "filer_name": "Joint CEO",
            "role": "officer (CEO)",
        },
        {
            "filer_cik": "9002",
            "filer_name": "Joint CFO",
            "role": "officer (CFO)",
        },
    ]

    assert _classify([joint_sale]) == []


def test_initial_bootstrap_respects_since_within_catchup_cap() -> None:
    sec = _FixtureSecAdapter([])
    stream = InsiderClusterStream(
        sec_adapter=sec,
        market_snapshot=lambda ticker: _snapshot(ticker=ticker),
        today=lambda: date(2026, 7, 31),
    )

    assert stream.fetch(None, since=date(2026, 7, 18)) == []

    assert sec.calls == [(date(2026, 7, 18), date(2026, 7, 29))]


def test_publication_lag_fixture_acceptance_uses_catchup_from_since() -> None:
    rows = [
        _row(
            transaction_date="2026-07-15",
            filed_at="2026-07-16",
        ),
        _second_buyer(
            transaction_date="2026-07-28",
            filed_at="2026-07-28",
        ),
    ]
    sec = _FixtureSecAdapter(rows)
    stream = InsiderClusterStream(
        sec_adapter=sec,
        market_snapshot=lambda ticker: _snapshot(ticker=ticker),
        today=lambda: date(2026, 7, 31),
    )

    nominations = stream.fetch(None, since=date(2026, 7, 30))

    # since after through clamps to the completed SEC day
    assert sec.calls == [(date(2026, 7, 29), date(2026, 7, 29))]
    assert nominations == []


def test_twenty_day_outage_expands_network_range_within_catchup_cap() -> None:
    rows = [
        _row(
            transaction_date="2026-07-20",
            filed_at="2026-07-21",
        ),
        _second_buyer(
            transaction_date="2026-07-22",
            filed_at="2026-07-22",
        ),
    ]
    sec = _FixtureSecAdapter(rows)
    stream = InsiderClusterStream(
        sec_adapter=sec,
        market_snapshot=lambda ticker: _snapshot(ticker=ticker),
        today=lambda: date(2026, 7, 31),
    )

    nominations = stream.fetch(None, since=date(2026, 7, 10))

    assert sec.calls == [(date(2026, 7, 10), date(2026, 7, 29))]
    assert [nomination.ticker for nomination in nominations] == ["ACME"]


def test_insider_fetch_clamps_historical_since_to_catchup_cap() -> None:
    sec = _FixtureSecAdapter([])
    stream = InsiderClusterStream(
        sec_adapter=sec,
        market_snapshot=lambda ticker: _snapshot(ticker=ticker),
        today=lambda: date(2026, 7, 31),
    )

    stream.fetch(None, since=date(2026, 5, 1))

    assert MAX_GLOBAL_DATE_RANGE_DAYS == 45
    assert sec.calls == [(date(2026, 6, 29), date(2026, 7, 29))]


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
        today=lambda: date(2023, 3, 16),
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
    assert sec.calls == [(date(2023, 3, 14), date(2023, 3, 14))]


def test_sec_adapter_outage_fails_the_stream() -> None:
    class FailingSecAdapter:
        async def get_form4_for_date_range(
            self,
            start_date,
            through=None,
        ):
            raise MissingDataSourceError("SEC unavailable")

    stream = InsiderClusterStream(
        sec_adapter=FailingSecAdapter(),
        market_snapshot=lambda ticker: _snapshot(ticker=ticker),
        today=lambda: date(2026, 7, 11),
    )
    with pytest.raises(MissingDataSourceError, match="SEC unavailable"):
        stream.fetch(None, since=date(2026, 7, 9))
