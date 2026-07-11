"""Stream B daily-pull persistence and local-window integration tests."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.config import InsiderClusterSignalConfig
from argosy.orchestrator.loops import signal_streams_daily as daily_loop_mod
from argosy.services.signal_streams import insider as insider_mod
from argosy.services.signal_streams.insider import (
    InsiderClusterConfig,
    InsiderClusterStream,
    InsiderMarketSnapshot,
    latest_completed_sec_day,
)
from argosy.services.signal_streams.pipeline import process_nominations
from argosy.state.models import (
    Base,
    MonitorFlag,
    Prediction,
    SignalStreamCursor,
    SignalStreamEvent,
    User,
)


def _row(
    *,
    accession: str,
    owner_cik: str,
    owner_name: str,
    transaction_date: str,
    filed_at: str,
    value_usd: float,
    transaction_index: int = 0,
    ticker: str = "ACME",
    issuer_cik: str = "0000009999",
    is_amendment: bool = False,
    date_of_original_submission: str = "",
    cluster_eligible: bool = True,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "issuer_cik": issuer_cik,
        "accession": accession,
        "filing_identity": accession,
        "transaction_index": transaction_index,
        "filer_cik": owner_cik,
        "filer_name": owner_name,
        "reporting_owners": [
            {
                "filer_cik": owner_cik,
                "filer_name": owner_name,
                "role": "director",
            }
        ],
        "role": "director",
        "transaction_date": transaction_date,
        "filed_at": filed_at,
        "transaction_code": "P",
        "acquired_disposed_code": "A",
        "shares": value_usd / 100,
        "price_per_share": 100,
        "value_usd": value_usd,
        "post_transaction_holdings": 10_000,
        "security_title": "Common Stock",
        "direct_or_indirect_ownership": "D",
        "nature_of_ownership": "",
        "is_derivative": False,
        "is_10b5_1": False,
        "tenb5_1_evidence": [],
        "document_has_10b5_1": False,
        "document_10b5_1_evidence": [],
        "cluster_eligible": cluster_eligible,
        "is_amendment": is_amendment,
        "document_type": "4/A" if is_amendment else "4",
        "date_of_original_submission": date_of_original_submission,
        "amendment_match_status": "unmatched" if is_amendment else "not_amendment",
        "amendment_ambiguity_evidence": [],
        "source_urls": [f"https://www.sec.gov/Archives/{accession}.txt"],
    }


class _DailySec:
    def __init__(self, rows_by_day: dict[date, list[dict[str, Any]]]) -> None:
        self.rows_by_day = rows_by_day
        self.calls: list[tuple[date, date | None]] = []

    async def get_form4_for_date_range(
        self,
        start_date: date,
        through: date | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((start_date, through))
        end = through or start_date
        if end < start_date:
            return []
        rows: list[dict[str, Any]] = []
        current = start_date
        while current <= end:
            rows.extend(dict(row) for row in self.rows_by_day.get(current, []))
            current += timedelta(days=1)
        return rows


def _snapshot(ticker: str) -> InsiderMarketSnapshot:
    return InsiderMarketSnapshot(
        price=25,
        market_cap=100_000_000,
        average_volume=2_000_000,
        quote_source_url=f"https://finance.yahoo.com/quote/{ticker}",
    )


@pytest.fixture
def ledger(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'insider-events.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                User(id="ariel", plan="free"),
                User(id="noga", plan="free"),
            ]
        )
        session.commit()
    try:
        yield engine, factory
    finally:
        engine.dispose()


def test_daily_pull_config_is_exactly_one_day() -> None:
    assert InsiderClusterConfig().daily_pull_days == 1
    assert InsiderClusterSignalConfig().daily_pull_days == 1
    assert InsiderClusterSignalConfig().enabled is False
    assert InsiderClusterConfig().ledger_horizon_days == 45
    assert InsiderClusterSignalConfig().ledger_horizon_days == 45
    with pytest.raises(ValueError, match="daily_pull_days"):
        InsiderClusterConfig(daily_pull_days=2)
    with pytest.raises(ValueError, match="daily_pull_days"):
        InsiderClusterSignalConfig(daily_pull_days=2)
    with pytest.raises(ValueError, match="ledger_horizon_days"):
        InsiderClusterConfig(ledger_horizon_days=46)
    with pytest.raises(ValueError, match="ledger_horizon_days"):
        InsiderClusterSignalConfig(ledger_horizon_days=13)


@pytest.mark.parametrize(
    ("today", "lag_days", "expected"),
    [
        (date(2026, 7, 13), 2, date(2026, 7, 10)),  # weekend
        (date(2026, 7, 5), 2, date(2026, 7, 2)),  # observed July 4 closure
        (date(2026, 7, 9), 1, date(2026, 7, 8)),
    ],
)
def test_latest_completed_sec_day_skips_weekends_and_federal_closures(
    today: date,
    lag_days: int,
    expected: date,
) -> None:
    assert latest_completed_sec_day(today, lag_days=lag_days) == expected


def test_two_daily_pulls_accumulate_locally_and_rerun_is_idempotent(
    ledger,
) -> None:
    _engine, factory = ledger
    clock = {"today": date(2026, 7, 8)}
    first_day = date(2026, 7, 7)
    second_day = date(2026, 7, 8)
    sec = _DailySec(
        {
            first_day: [
                _row(
                    accession="first",
                    owner_cik="0000001001",
                    owner_name="Alice",
                    transaction_date=first_day.isoformat(),
                    filed_at=first_day.isoformat(),
                    value_usd=60_000,
                )
            ],
            second_day: [
                _row(
                    accession="second",
                    owner_cik="0000001002",
                    owner_name="Bob",
                    transaction_date=second_day.isoformat(),
                    filed_at=second_day.isoformat(),
                    value_usd=40_000,
                )
            ],
        }
    )
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=_snapshot,
        today=lambda: clock["today"],
        observed_at=lambda: datetime(2026, 7, 9, 12, tzinfo=UTC),
    )

    with factory() as session:
        assert stream.fetch(session, since=first_day) == []
        session.commit()
    assert sec.calls == [(first_day, first_day)]

    clock["today"] = date(2026, 7, 9)
    with factory() as session:
        nominations = stream.fetch(session, since=second_day)
        session.commit()
    assert len(nominations) == 1
    assert nominations[0].as_of == second_day
    assert {item["accession"] for item in nominations[0].evidence["transactions"]} == {
        "first",
        "second",
    }

    with factory() as session:
        assert stream.fetch(session, since=second_day) == []
        session.commit()
        events = session.query(SignalStreamEvent).order_by(SignalStreamEvent.id).all()
        assert len(events) == 2
        assert all(event.active == 1 for event in events)
    assert sec.calls == [
        (first_day, first_day),
        (second_day, second_day),
        (second_day, second_day),
    ]


def test_matched_amendment_replaces_group_and_changes_dedup_when_row_removed(
    ledger,
) -> None:
    _engine, factory = ledger
    clock = {"today": date(2026, 7, 8)}
    original_day = date(2026, 7, 7)
    amendment_day = date(2026, 7, 8)
    original = _row(
        accession="original",
        owner_cik="0000001001",
        owner_name="Alice",
        transaction_date="2026-07-06",
        filed_at=original_day.isoformat(),
        value_usd=30_000,
    )
    original_second = {
        **original,
        "transaction_index": 1,
        "value_usd": 30_000,
        "shares": 300,
    }
    other = _row(
        accession="other",
        owner_cik="0000001002",
        owner_name="Bob",
        transaction_date="2026-07-07",
        filed_at=original_day.isoformat(),
        value_usd=40_000,
    )
    amendment = _row(
        accession="amendment",
        owner_cik="0000001001",
        owner_name="Alice",
        transaction_date="2026-07-06",
        filed_at=amendment_day.isoformat(),
        value_usd=60_000,
        is_amendment=True,
        date_of_original_submission=original_day.isoformat(),
        cluster_eligible=False,
    )
    sec = _DailySec(
        {
            original_day: [original, original_second, other],
            amendment_day: [amendment],
        }
    )
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=_snapshot,
        today=lambda: clock["today"],
    )

    with factory() as session:
        first = stream.fetch(session, since=original_day)
        session.commit()
    assert len(first) == 1

    clock["today"] = date(2026, 7, 9)
    with factory() as session:
        amended = stream.fetch(session, since=amendment_day)
        session.commit()
        events = session.query(SignalStreamEvent).order_by(SignalStreamEvent.id).all()

    assert len(amended) == 1
    assert amended[0].dedup_key != first[0].dedup_key
    alice = [
        event
        for event in events
        if "0000001001" in event.payload_json
    ]
    assert len(alice) == 2
    assert [event.active for event in alice] == [1, 0]
    amended_payload = next(event for event in alice if event.active == 1)
    assert '"accession":"amendment"' in amended_payload.payload_json
    assert (
        json.loads(amended_payload.payload_json)["amendment_match_status"]
        == "matched"
    )
    assert json.loads(amended_payload.source_urls_json) == [
        "https://www.sec.gov/Archives/amendment.txt",
        "https://www.sec.gov/Archives/original.txt",
    ]


def test_unmatched_amendment_taints_overlapping_local_group_only(ledger) -> None:
    _engine, factory = ledger
    clock = {"today": date(2026, 7, 8)}
    original_day = date(2026, 7, 7)
    amendment_day = date(2026, 7, 8)
    sec = _DailySec(
        {
            original_day: [
                _row(
                    accession="affected",
                    owner_cik="0000001001",
                    owner_name="Alice",
                    transaction_date="2026-07-06",
                    filed_at=original_day.isoformat(),
                    value_usd=60_000,
                ),
                _row(
                    accession="unrelated",
                    owner_cik="0000001002",
                    owner_name="Bob",
                    transaction_date="2026-07-06",
                    filed_at=original_day.isoformat(),
                    value_usd=40_000,
                ),
            ],
            amendment_day: [
                _row(
                    accession="unknown-amendment",
                    owner_cik="0000001001",
                    owner_name="Alice",
                    transaction_date="2026-07-06",
                    filed_at=amendment_day.isoformat(),
                    value_usd=65_000,
                    is_amendment=True,
                    date_of_original_submission="",
                    cluster_eligible=False,
                )
            ],
        }
    )
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=_snapshot,
        today=lambda: clock["today"],
    )

    with factory() as session:
        assert len(stream.fetch(session, since=original_day)) == 1
        session.commit()
    clock["today"] = date(2026, 7, 9)
    with factory() as session:
        assert stream.fetch(session, since=amendment_day) == []
        session.commit()
        events = session.query(SignalStreamEvent).all()

    active_accessions = {
        json.loads(event.payload_json)["accession"]
        for event in events
        if event.active == 1
    }
    assert active_accessions == {"unrelated"}
    assert {
        json.loads(event.payload_json)["accession"]
        for event in events
        if event.active == 0
    } == {"affected", "unknown-amendment"}
    unknown = next(
        event
        for event in events
        if json.loads(event.payload_json)["accession"] == "unknown-amendment"
    )
    assert json.loads(unknown.payload_json)["amendment_match_status"] == "ambiguous"


def test_corrected_amendment_restores_recent_tainted_original_group(ledger) -> None:
    _engine, factory = ledger
    original_day = date(2026, 7, 7)
    taint_day = date(2026, 7, 8)
    correction_day = date(2026, 7, 9)
    clock = {"today": date(2026, 7, 8)}
    original = _row(
        accession="restore-original",
        owner_cik="0000001001",
        owner_name="Alice",
        transaction_date="2026-07-06",
        filed_at=original_day.isoformat(),
        value_usd=30_000,
    )
    removed = {
        **original,
        "transaction_index": 1,
        "value_usd": 30_000,
        "shares": 300,
    }
    ownerless_taint = _row(
        accession="restore-ownerless",
        owner_cik="",
        owner_name="",
        transaction_date="2026-07-06",
        filed_at=taint_day.isoformat(),
        value_usd=60_000,
        is_amendment=True,
        date_of_original_submission=original_day.isoformat(),
        cluster_eligible=False,
    )
    ownerless_taint["reporting_owners"] = [
        {"filer_cik": "", "filer_name": "", "role": "director"}
    ]
    corrected = _row(
        accession="restore-corrected",
        owner_cik="0000001001",
        owner_name="Alice",
        transaction_date="2026-07-06",
        filed_at=correction_day.isoformat(),
        value_usd=65_000,
        is_amendment=True,
        date_of_original_submission=original_day.isoformat(),
        cluster_eligible=False,
    )
    sec = _DailySec(
        {
            original_day: [original, removed],
            taint_day: [ownerless_taint],
            correction_day: [corrected],
        }
    )
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=_snapshot,
        today=lambda: clock["today"],
    )

    with factory() as session:
        stream.fetch(session, since=original_day)
        session.commit()
    clock["today"] = date(2026, 7, 9)
    with factory() as session:
        stream.fetch(session, since=taint_day)
        session.commit()
        assert all(
            event.active == 0
            for event in session.query(SignalStreamEvent).all()
        )
    clock["today"] = date(2026, 7, 10)
    with factory() as session:
        stream.fetch(session, since=correction_day)
        session.commit()
        events = session.query(SignalStreamEvent).all()

    stable_group = [
        event
        for event in events
        if event.event_group_key == "sec-form4:restore-original"
    ]
    assert len(stable_group) == 2
    active = [event for event in stable_group if event.active == 1]
    assert len(active) == 1
    assert json.loads(active[0].payload_json)["accession"] == "restore-corrected"
    assert active[0].event_key.endswith(":non_derivative:0")
    removed_event = next(
        event
        for event in stable_group
        if event.event_key.endswith(":non_derivative:1")
    )
    assert removed_event.active == 0
    assert all(
        event.active == 0
        for event in events
        if event.event_group_key == "sec-form4:restore-ownerless"
    )


def test_ownerless_amendment_taint_restores_collateral_sibling_when_alice_resolves(
    ledger,
) -> None:
    """Replay B3: Alice+Bob tainted by ownerless amendment; Alice correction
    must restore Bob (live taint recompute), not leave the sibling dead."""
    _engine, factory = ledger
    original_day = date(2026, 7, 7)
    taint_day = date(2026, 7, 8)
    correction_day = date(2026, 7, 9)
    clock = {"today": date(2026, 7, 8)}
    alice = _row(
        accession="alice-original",
        owner_cik="0000001001",
        owner_name="Alice",
        transaction_date="2026-07-06",
        filed_at=original_day.isoformat(),
        value_usd=60_000,
    )
    bob = _row(
        accession="bob-original",
        owner_cik="0000001002",
        owner_name="Bob",
        transaction_date="2026-07-06",
        filed_at=original_day.isoformat(),
        value_usd=40_000,
    )
    ownerless_taint = _row(
        accession="ownerless-amendment",
        owner_cik="",
        owner_name="",
        transaction_date="2026-07-06",
        filed_at=taint_day.isoformat(),
        value_usd=60_000,
        is_amendment=True,
        date_of_original_submission=original_day.isoformat(),
        cluster_eligible=False,
    )
    ownerless_taint["reporting_owners"] = [
        {"filer_cik": "", "filer_name": "", "role": "director"}
    ]
    alice_correction = _row(
        accession="alice-correction",
        owner_cik="0000001001",
        owner_name="Alice",
        transaction_date="2026-07-06",
        filed_at=correction_day.isoformat(),
        value_usd=65_000,
        is_amendment=True,
        date_of_original_submission=original_day.isoformat(),
        cluster_eligible=False,
    )
    sec = _DailySec(
        {
            original_day: [alice, bob],
            taint_day: [ownerless_taint],
            correction_day: [alice_correction],
        }
    )
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=_snapshot,
        today=lambda: clock["today"],
    )

    with factory() as session:
        assert len(stream.fetch(session, since=original_day)) == 1
        session.commit()
    clock["today"] = date(2026, 7, 9)
    with factory() as session:
        assert stream.fetch(session, since=taint_day) == []
        session.commit()
        assert {
            json.loads(event.payload_json)["accession"]
            for event in session.query(SignalStreamEvent).all()
            if event.active == 1
        } == set()
    clock["today"] = date(2026, 7, 10)
    with factory() as session:
        nominations = stream.fetch(session, since=correction_day)
        session.commit()
        events = session.query(SignalStreamEvent).all()

    active_accessions = {
        json.loads(event.payload_json)["accession"]
        for event in events
        if event.active == 1
    }
    assert "alice-correction" in active_accessions
    assert "bob-original" in active_accessions
    assert "alice-original" not in active_accessions
    assert "ownerless-amendment" not in active_accessions
    assert len(nominations) == 1
    assert nominations[0].ticker == "ACME"


def test_adapter_matched_same_day_amendment_is_locally_eligible(ledger) -> None:
    _engine, factory = ledger
    day = date(2026, 7, 7)
    amendment = _row(
        accession="same-day-amendment",
        owner_cik="0000001001",
        owner_name="Alice",
        transaction_date=day.isoformat(),
        filed_at=day.isoformat(),
        value_usd=60_000,
        is_amendment=True,
        date_of_original_submission=day.isoformat(),
        cluster_eligible=False,
    )
    amendment["amendment_match_status"] = "matched"
    amendment["filing_identity"] = "same-day-original"
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=_DailySec({day: [amendment]}),
        market_snapshot=_snapshot,
        today=lambda: date(2026, 7, 8),
    )

    with factory() as session:
        assert stream.fetch(session, since=day) == []
        session.commit()
        event = session.query(SignalStreamEvent).one()
        assert event.active == 1
        assert event.event_group_key == "sec-form4:same-day-original"
        assert json.loads(event.payload_json)["cluster_eligible"] is True


def test_distinct_same_day_originals_survive_and_later_amendment_is_ambiguous(
    ledger,
) -> None:
    _engine, factory = ledger
    clock = {"today": date(2026, 7, 8)}
    original_day = date(2026, 7, 7)
    amendment_day = date(2026, 7, 8)
    originals = [
        _row(
            accession=accession,
            owner_cik="0000001001",
            owner_name="Alice",
            transaction_date="2026-07-06",
            filed_at=original_day.isoformat(),
            value_usd=value,
        )
        for accession, value in (("original-a", 60_000), ("original-b", 40_000))
    ]
    amendment = _row(
        accession="ambiguous-amendment",
        owner_cik="0000001001",
        owner_name="Alice",
        transaction_date="2026-07-06",
        filed_at=amendment_day.isoformat(),
        value_usd=65_000,
        is_amendment=True,
        date_of_original_submission=original_day.isoformat(),
        cluster_eligible=False,
    )
    sec = _DailySec(
        {
            original_day: originals,
            amendment_day: [amendment],
        }
    )
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=_snapshot,
        today=lambda: clock["today"],
    )

    with factory() as session:
        assert stream.fetch(session, since=original_day) == []
        session.commit()
        original_events = session.query(SignalStreamEvent).all()
        assert len(original_events) == 2
        assert {event.event_group_key for event in original_events} == {
            "sec-form4:original-a",
            "sec-form4:original-b",
        }

    clock["today"] = date(2026, 7, 9)
    with factory() as session:
        assert stream.fetch(session, since=amendment_day) == []
        session.commit()
        events = session.query(SignalStreamEvent).all()

    assert len(events) == 3
    assert all(event.active == 0 for event in events)
    amendment_event = next(
        event
        for event in events
        if json.loads(event.payload_json)["accession"] == "ambiguous-amendment"
    )
    evidence = json.loads(amendment_event.payload_json)
    assert evidence["_signal_resolution_reason"] == "multiple_original_candidates"
    assert evidence["_signal_candidate_groups"] == [
        "sec-form4:original-a",
        "sec-form4:original-b",
    ]


def test_same_day_batch_amendment_resolves_unique_original(ledger) -> None:
    _engine, factory = ledger
    day = date(2026, 7, 7)
    original = _row(
        accession="same-day-original",
        owner_cik="0000001001",
        owner_name="Alice",
        transaction_date=day.isoformat(),
        filed_at=day.isoformat(),
        value_usd=60_000,
    )
    amendment = _row(
        accession="same-day-amendment",
        owner_cik="0000001001",
        owner_name="Alice",
        transaction_date=day.isoformat(),
        filed_at=day.isoformat(),
        value_usd=65_000,
        is_amendment=True,
        date_of_original_submission=day.isoformat(),
        cluster_eligible=False,
    )
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=_DailySec({day: [original, amendment]}),
        market_snapshot=_snapshot,
        today=lambda: date(2026, 7, 8),
    )

    with factory() as session:
        assert stream.fetch(session, since=day) == []
        session.commit()
        event = session.query(SignalStreamEvent).one()

    payload = json.loads(event.payload_json)
    assert event.event_group_key == "sec-form4:same-day-original"
    assert event.active == 1
    assert payload["accession"] == "same-day-amendment"
    assert payload["amendment_match_status"] == "matched"


def test_market_snapshot_failure_retries_pending_local_cluster(ledger) -> None:
    _engine, factory = ledger
    day = date(2026, 7, 7)
    sec = _DailySec(
        {
            day: [
                _row(
                    accession="retry-a",
                    owner_cik="0000001001",
                    owner_name="Alice",
                    transaction_date=day.isoformat(),
                    filed_at=day.isoformat(),
                    value_usd=60_000,
                ),
                _row(
                    accession="retry-b",
                    owner_cik="0000001002",
                    owner_name="Bob",
                    transaction_date=day.isoformat(),
                    filed_at=day.isoformat(),
                    value_usd=40_000,
                ),
            ]
        }
    )
    snapshot_attempts = 0
    clock = {"today": date(2026, 7, 8)}

    def flaky_snapshot(ticker: str) -> InsiderMarketSnapshot:
        nonlocal snapshot_attempts
        snapshot_attempts += 1
        if snapshot_attempts == 1:
            raise TimeoutError("market cache unavailable")
        return _snapshot(ticker)

    observed = {"at": datetime(2026, 7, 8, 12, tzinfo=UTC)}
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=flaky_snapshot,
        today=lambda: clock["today"],
        observed_at=lambda: observed["at"],
    )

    with factory() as session:
        assert stream.fetch(session, since=day) == []
        session.commit()
        assert {
            event.evaluation_pending
            for event in session.query(SignalStreamEvent).all()
        } == {1}
        first_seen_times = {
            event.event_key: event.last_seen_at
            for event in session.query(SignalStreamEvent).all()
        }

    clock["today"] = date(2026, 7, 9)
    observed["at"] = datetime(2026, 7, 9, 12, tzinfo=UTC)
    next_day = date(2026, 7, 8)
    with factory() as session:
        recovered = stream.fetch(session, since=next_day)
        assert len(recovered) == 1
        process_nominations(
            session,
            user_id="ariel",
            nominations=recovered,
            observed_at=observed["at"],
        )
        session.commit()
        assert {
            event.evaluation_pending
            for event in session.query(SignalStreamEvent).all()
        } == {0}
        assert {
            event.event_key: event.last_seen_at
            for event in session.query(SignalStreamEvent).all()
        } == first_seen_times
        assert session.query(Prediction).count() == 2

    with factory() as session:
        assert stream.fetch(session, since=next_day) == []
        process_nominations(
            session,
            user_id="ariel",
            nominations=[],
            observed_at=observed["at"],
        )
        session.commit()
        assert session.query(Prediction).count() == 2
    assert sec.calls == [(day, day), (next_day, next_day), (next_day, next_day)]
    assert snapshot_attempts == 2


def test_missing_price_keeps_buy_groups_pending_until_recovery(ledger) -> None:
    _engine, factory = ledger
    day = date(2026, 7, 7)
    next_day = date(2026, 7, 8)
    clock = {"today": date(2026, 7, 8)}
    sec = _DailySec(
        {
            day: [
                _row(
                    accession="price-a",
                    owner_cik="0000001001",
                    owner_name="Alice",
                    transaction_date=day.isoformat(),
                    filed_at=day.isoformat(),
                    value_usd=60_000,
                ),
                _row(
                    accession="price-b",
                    owner_cik="0000001002",
                    owner_name="Bob",
                    transaction_date=day.isoformat(),
                    filed_at=day.isoformat(),
                    value_usd=40_000,
                ),
            ]
        }
    )
    calls = 0

    def price_then_recover(ticker: str) -> InsiderMarketSnapshot:
        nonlocal calls
        calls += 1
        return (
            InsiderMarketSnapshot(
                price=None,
                market_cap=100_000_000,
                average_volume=2_000_000,
                quote_source_url=f"https://finance.yahoo.com/quote/{ticker}",
            )
            if calls == 1
            else _snapshot(ticker)
        )

    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=price_then_recover,
        today=lambda: clock["today"],
    )
    with factory() as session:
        assert stream.fetch(session, since=day) == []
        session.commit()
        assert {
            event.evaluation_pending
            for event in session.query(SignalStreamEvent).all()
        } == {1}

    clock["today"] = date(2026, 7, 9)
    with factory() as session:
        recovered = stream.fetch(session, since=next_day)
        session.commit()
        assert len(recovered) == 1
        assert recovered[0].direction == "long"
        assert {
            event.evaluation_pending
            for event in session.query(SignalStreamEvent).all()
        } == {0}
    assert calls == 2


def test_missing_market_cap_keeps_buy_groups_pending_until_recovery(
    ledger,
) -> None:
    _engine, factory = ledger
    day = date(2026, 7, 7)
    clock = {"today": date(2026, 7, 8)}
    sec = _DailySec(
        {
            day: [
                _row(
                    accession="cap-a",
                    owner_cik="0000001001",
                    owner_name="Alice",
                    transaction_date=day.isoformat(),
                    filed_at=day.isoformat(),
                    value_usd=60_000,
                ),
                _row(
                    accession="cap-b",
                    owner_cik="0000001002",
                    owner_name="Bob",
                    transaction_date=day.isoformat(),
                    filed_at=day.isoformat(),
                    value_usd=40_000,
                ),
            ]
        }
    )
    calls = 0

    def cap_then_recover(ticker: str) -> InsiderMarketSnapshot:
        nonlocal calls
        calls += 1
        return (
            InsiderMarketSnapshot(
                price=25,
                market_cap=None,
                average_volume=2_000_000,
                quote_source_url=f"https://finance.yahoo.com/quote/{ticker}",
            )
            if calls == 1
            else _snapshot(ticker)
        )

    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=cap_then_recover,
        today=lambda: clock["today"],
    )
    with factory() as session:
        assert stream.fetch(session, since=day) == []
        session.commit()
        assert {
            event.evaluation_pending
            for event in session.query(SignalStreamEvent).all()
        } == {1}

    clock["today"] = date(2026, 7, 9)
    with factory() as session:
        recovered = stream.fetch(session, since=date(2026, 7, 8))
        session.commit()
        assert len(recovered) == 1
        assert recovered[0].direction == "long"
        assert {
            event.evaluation_pending
            for event in session.query(SignalStreamEvent).all()
        } == {0}
    assert calls == 2


def test_missing_market_cap_processes_warning_without_later_duplicate(
    ledger,
) -> None:
    _engine, factory = ledger
    day = date(2026, 7, 7)
    clock = {"today": date(2026, 7, 8)}

    def seller(
        *,
        accession: str,
        owner_cik: str,
        owner_name: str,
        role: str,
    ) -> dict[str, Any]:
        row = _row(
            accession=accession,
            owner_cik=owner_cik,
            owner_name=owner_name,
            transaction_date=day.isoformat(),
            filed_at=day.isoformat(),
            value_usd=3_000,
        )
        row.update(
            {
                "role": role,
                "transaction_code": "S",
                "acquired_disposed_code": "D",
                "shares": 300,
                "price_per_share": 10,
                "post_transaction_holdings": 700,
            }
        )
        row["reporting_owners"][0]["role"] = role
        return row

    sec = _DailySec(
        {
            day: [
                seller(
                    accession="sell-a",
                    owner_cik="0000001001",
                    owner_name="Alice",
                    role="officer (CEO)",
                ),
                seller(
                    accession="sell-b",
                    owner_cik="0000001002",
                    owner_name="Bob",
                    role="officer (CFO)",
                ),
            ]
        }
    )
    snapshot_calls = 0

    def price_without_cap(ticker: str) -> InsiderMarketSnapshot:
        nonlocal snapshot_calls
        snapshot_calls += 1
        return InsiderMarketSnapshot(
            price=25,
            market_cap=None,
            average_volume=2_000_000,
            quote_source_url=f"https://finance.yahoo.com/quote/{ticker}",
        )

    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=price_without_cap,
        today=lambda: clock["today"],
    )
    with factory() as session:
        warnings = stream.fetch(session, since=day)
        assert len(warnings) == 1
        assert warnings[0].direction == "short"
        process_nominations(
            session,
            user_id="ariel",
            nominations=warnings,
        )
        session.commit()
        assert {
            event.evaluation_pending
            for event in session.query(SignalStreamEvent).all()
        } == {0}
        assert session.query(Prediction).count() == 2
        assert session.query(MonitorFlag).count() == 1

    clock["today"] = date(2026, 7, 9)
    with factory() as session:
        assert stream.fetch(session, since=date(2026, 7, 8)) == []
        session.commit()
        assert session.query(Prediction).count() == 2
        assert session.query(MonitorFlag).count() == 1
    assert snapshot_calls == 1


@pytest.mark.parametrize(
    ("original_date", "tainted_accessions", "reason"),
    [
        (
            "2026-07-07",
            {"dated-a", "dated-b"},
            "ownerless_original_date_scope",
        ),
        (
            "",
            {"dated-a", "dated-b", "other-date"},
            "ownerless_issuer_lookback_scope",
        ),
    ],
)
def test_ownerless_amendment_taints_scoped_same_issuer_groups(
    ledger,
    original_date: str,
    tainted_accessions: set[str],
    reason: str,
) -> None:
    _engine, factory = ledger
    first_day = date(2026, 7, 7)
    amendment_day = date(2026, 7, 8)
    clock = {"today": date(2026, 7, 8)}
    observed = {"at": datetime(2026, 7, 8, 12, tzinfo=UTC)}
    originals = [
        _row(
            accession="dated-a",
            owner_cik="0000001001",
            owner_name="Alice",
            transaction_date="2026-07-06",
            filed_at=first_day.isoformat(),
            value_usd=30_000,
        ),
        _row(
            accession="dated-b",
            owner_cik="0000001002",
            owner_name="Bob",
            transaction_date="2026-07-06",
            filed_at=first_day.isoformat(),
            value_usd=30_000,
        ),
        _row(
            accession="other-date",
            owner_cik="0000001003",
            owner_name="Cara",
            transaction_date="2026-07-05",
            filed_at="2026-07-06",
            value_usd=30_000,
        ),
        _row(
            accession="other-issuer",
            owner_cik="0000002001",
            owner_name="Dana",
            issuer_cik="0000008888",
            transaction_date="2026-07-06",
            filed_at=first_day.isoformat(),
            value_usd=30_000,
        ),
    ]
    amendment = _row(
        accession=f"ownerless-{reason}",
        owner_cik="",
        owner_name="",
        transaction_date="2026-07-06",
        filed_at=amendment_day.isoformat(),
        value_usd=65_000,
        is_amendment=True,
        date_of_original_submission=original_date,
        cluster_eligible=False,
    )
    amendment["reporting_owners"] = [
        {"filer_cik": "", "filer_name": "", "role": "director"}
    ]
    sec = _DailySec(
        {
            first_day: originals,
            amendment_day: [amendment],
        }
    )
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=_snapshot,
        today=lambda: clock["today"],
        observed_at=lambda: observed["at"],
    )

    with factory() as session:
        stream.fetch(session, since=first_day)
        session.commit()
        prior_seen = {
            json.loads(event.payload_json)["accession"]: event.last_seen_at
            for event in session.query(SignalStreamEvent).all()
        }
    clock["today"] = date(2026, 7, 9)
    observed["at"] = datetime(2026, 7, 9, 12, tzinfo=UTC)
    with factory() as session:
        assert stream.fetch(session, since=amendment_day) == []
        session.commit()
        events = session.query(SignalStreamEvent).all()

    by_accession = {
        json.loads(event.payload_json)["accession"]: event
        for event in events
    }
    assert all(by_accession[accession].active == 0 for accession in tainted_accessions)
    assert all(
        by_accession[accession].last_seen_at == prior_seen[accession]
        for accession in tainted_accessions
    )
    expected_active = {"other-issuer", "other-date"} - tainted_accessions
    assert {
        accession
        for accession, event in by_accession.items()
        if event.active == 1
    } == expected_active
    amendment_payload = json.loads(
        by_accession[f"ownerless-{reason}"].payload_json
    )
    assert amendment_payload["_signal_resolution_reason"] == reason
    assert amendment_payload["_signal_candidate_groups"] == sorted(
        by_accession[accession].event_group_key
        for accession in tainted_accessions
    )


def test_absent_replaced_rows_keep_prior_last_seen_at(ledger) -> None:
    _engine, factory = ledger
    original_day = date(2026, 7, 7)
    amendment_day = date(2026, 7, 8)
    clock = {
        "today": date(2026, 7, 8),
        "observed": datetime(2026, 7, 8, 12, tzinfo=UTC),
    }
    original = _row(
        accession="seen-original",
        owner_cik="0000001001",
        owner_name="Alice",
        transaction_date="2026-07-06",
        filed_at=original_day.isoformat(),
        value_usd=30_000,
    )
    removed = {
        **original,
        "transaction_index": 1,
        "value_usd": 30_000,
        "shares": 300,
    }
    amendment = _row(
        accession="seen-amendment",
        owner_cik="0000001001",
        owner_name="Alice",
        transaction_date="2026-07-06",
        filed_at=amendment_day.isoformat(),
        value_usd=65_000,
        is_amendment=True,
        date_of_original_submission=original_day.isoformat(),
        cluster_eligible=False,
    )
    sec = _DailySec(
        {
            original_day: [original, removed],
            amendment_day: [amendment],
        }
    )
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=_snapshot,
        today=lambda: clock["today"],
        observed_at=lambda: clock["observed"],
    )

    with factory() as session:
        stream.fetch(session, since=original_day)
        session.commit()
        prior_seen = {
            json.loads(event.payload_json)["transaction_index"]: event.last_seen_at
            for event in session.query(SignalStreamEvent).all()
        }

    clock.update(
        {
            "today": date(2026, 7, 9),
            "observed": datetime(2026, 7, 9, 12, tzinfo=UTC),
        }
    )
    with factory() as session:
        stream.fetch(session, since=amendment_day)
        session.commit()
        events = {
            event.event_key.rsplit(":", 1)[-1]: event
            for event in session.query(SignalStreamEvent).all()
        }

    assert events["0"].last_seen_at == datetime(2026, 7, 9, 12)
    assert events["1"].active == 0
    assert events["1"].last_seen_at == prior_seen[1]


def test_event_ledger_is_isolated_by_user(ledger) -> None:
    _engine, factory = ledger
    day = date(2026, 7, 7)
    rows = [
        _row(
            accession="shared",
            owner_cik="0000001001",
            owner_name="Alice",
            transaction_date=day.isoformat(),
            filed_at=day.isoformat(),
            value_usd=60_000,
        )
    ]
    for user_id in ("ariel", "noga"):
        stream = InsiderClusterStream(
            user_id=user_id,
            config=InsiderClusterConfig(index_publication_lag_days=1),
            sec_adapter=_DailySec({day: rows}),
            market_snapshot=_snapshot,
            today=lambda: date(2026, 7, 8),
        )
        with factory() as session:
            assert stream.fetch(session, since=day) == []
            session.commit()

    with factory() as session:
        assert session.query(SignalStreamEvent).filter_by(user_id="ariel").count() == 1
        assert session.query(SignalStreamEvent).filter_by(user_id="noga").count() == 1


def test_market_cache_can_write_before_ledger_is_staged(ledger) -> None:
    engine, factory = ledger
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE market_cache_probe "
                "(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
        )
    clock = {"today": date(2026, 7, 8)}
    first_day = date(2026, 7, 7)
    second_day = date(2026, 7, 8)
    sec = _DailySec(
        {
            first_day: [
                _row(
                    accession="first",
                    owner_cik="0000001001",
                    owner_name="Alice",
                    transaction_date=first_day.isoformat(),
                    filed_at=first_day.isoformat(),
                    value_usd=60_000,
                )
            ],
            second_day: [
                _row(
                    accession="second",
                    owner_cik="0000001002",
                    owner_name="Bob",
                    transaction_date=second_day.isoformat(),
                    filed_at=second_day.isoformat(),
                    value_usd=40_000,
                )
            ],
        }
    )

    def write_through_snapshot(ticker: str) -> InsiderMarketSnapshot:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO market_cache_probe (id, value) "
                    "VALUES (1, :ticker)"
                ),
                {"ticker": ticker},
            )
        return _snapshot(ticker)

    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=write_through_snapshot,
        today=lambda: clock["today"],
    )
    with factory() as session:
        stream.fetch(session, since=first_day)
        session.commit()
    clock["today"] = date(2026, 7, 9)
    with factory() as session:
        assert len(stream.fetch(session, since=second_day)) == 1
        session.commit()
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT value FROM market_cache_probe WHERE id = 1")
        ).scalar_one() == "ACME"


def test_local_window_expires_old_events_and_unrelated_changes_do_not_repeat(
    ledger,
) -> None:
    _engine, factory = ledger
    clock = {"today": date(2026, 7, 2)}
    first_day = date(2026, 7, 1)
    second_day = date(2026, 7, 14)
    third_day = date(2026, 7, 15)
    sec = _DailySec(
        {
            first_day: [
                _row(
                    accession="old",
                    owner_cik="0000001001",
                    owner_name="Alice",
                    transaction_date=first_day.isoformat(),
                    filed_at=first_day.isoformat(),
                    value_usd=60_000,
                )
            ],
            second_day: [
                _row(
                    accession="edge",
                    owner_cik="0000001002",
                    owner_name="Bob",
                    transaction_date=second_day.isoformat(),
                    filed_at=second_day.isoformat(),
                    value_usd=40_000,
                )
            ],
            third_day: [
                _row(
                    accession="new",
                    owner_cik="0000001003",
                    owner_name="Cara",
                    transaction_date=third_day.isoformat(),
                    filed_at=third_day.isoformat(),
                    value_usd=20_000,
                ),
                _row(
                    accession="unrelated-ticker",
                    owner_cik="0000002001",
                    owner_name="Dana",
                    ticker="OTHER",
                    issuer_cik="0000008888",
                    transaction_date=third_day.isoformat(),
                    filed_at=third_day.isoformat(),
                    value_usd=200_000,
                ),
            ],
        }
    )
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=_snapshot,
        today=lambda: clock["today"],
    )
    with factory() as session:
        assert stream.fetch(session, since=first_day) == []
        session.commit()
    clock["today"] = date(2026, 7, 15)
    with factory() as session:
        assert len(stream.fetch(session, since=second_day)) == 1
        session.commit()
    clock["today"] = date(2026, 7, 16)
    with factory() as session:
        assert stream.fetch(session, since=third_day) == []
        session.commit()


def test_ledger_read_is_bounded_and_keeps_recent_inactive_and_pending_rows(
    ledger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine, factory = ledger
    through = date(2026, 7, 9)
    old_day = through.replace(month=5, day=1)
    recent_payload = _row(
        accession="recent",
        owner_cik="0000001001",
        owner_name="Alice",
        transaction_date=through.isoformat(),
        filed_at=through.isoformat(),
        value_usd=10_000,
    )
    with factory() as session:
        session.add_all(
            [
                SignalStreamEvent(
                    user_id="ariel",
                    stream="insider_cluster",
                    event_key=f"old-malformed-{index}",
                    event_group_key=f"old-group-{index}",
                    ticker="OLD",
                    event_at=old_day,
                    available_at=old_day,
                    payload_json="[]",
                    source_urls_json="[]",
                    active=index % 2,
                    evaluation_pending=index % 4,
                )
                for index in range(100)
            ]
            + [
                SignalStreamEvent(
                    user_id="ariel",
                    stream="insider_cluster",
                    event_key="future-malformed",
                    event_group_key="future-group",
                    ticker="FUTURE",
                    event_at=date(2026, 7, 10),
                    available_at=date(2026, 7, 10),
                    payload_json="[]",
                    source_urls_json="[]",
                    active=0,
                    evaluation_pending=0,
                ),
                SignalStreamEvent(
                    user_id="ariel",
                    stream="insider_cluster",
                    event_key="recent-inactive",
                    event_group_key="recent-inactive",
                    ticker="ACME",
                    event_at=through,
                    available_at=through,
                    payload_json=json.dumps(recent_payload),
                    source_urls_json="[]",
                    active=0,
                    evaluation_pending=0,
                ),
                SignalStreamEvent(
                    user_id="ariel",
                    stream="insider_cluster",
                    event_key="recent-pending",
                    event_group_key="recent-pending",
                    ticker="ACME",
                    event_at=through,
                    available_at=through,
                    payload_json=json.dumps(recent_payload),
                    source_urls_json="[]",
                    active=1,
                    evaluation_pending=1,
                ),
            ]
        )
        session.commit()

    seen_existing_keys: list[str] = []
    original_normalize = insider_mod._normalize_daily_rows

    def capture_existing(rows, **kwargs):
        seen_existing_keys.extend(
            event.event_key for event in kwargs["existing_events"]
        )
        return original_normalize(rows, **kwargs)

    monkeypatch.setattr(
        insider_mod,
        "_normalize_daily_rows",
        capture_existing,
    )
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=_DailySec({through: []}),
        market_snapshot=_snapshot,
        today=lambda: date(2026, 7, 10),
    )

    with factory() as session:
        assert stream.fetch(session, since=through) == []

    assert set(seen_existing_keys) == {
        "recent-inactive",
        "recent-pending",
    }


def test_loop_treats_insider_cursor_as_audit_only(ledger) -> None:
    _engine, factory = ledger

    class AuditOnlyInsider:
        name = "insider_cluster"
        cursor_controls_fetch_range = False
        config = InsiderClusterConfig()

        def __init__(self) -> None:
            self.since: list[date] = []

        def fetch(self, session, *, since):
            self.since.append(since)
            return []

    with factory() as session:
        session.add(
            SignalStreamCursor(
                user_id="ariel",
                stream="insider_cluster",
                last_success_at=datetime(2026, 6, 1, 12, tzinfo=UTC),
            )
        )
        session.commit()
    stream = AuditOnlyInsider()
    loop = daily_loop_mod.SignalStreamsDailyLoop(
        streams=[stream],
        session_factory=factory,
        user_id="ariel",
    )

    result = loop._run_sync(datetime(2026, 7, 10, 12, tzinfo=UTC))

    assert result["streams"]["insider_cluster"]["status"] == "ok"
    assert stream.since == [date(2026, 7, 10)]
    with factory() as session:
        cursor = session.get(
            SignalStreamCursor,
            ("ariel", "insider_cluster"),
        )
        assert cursor is not None
        assert cursor.last_success_at == datetime(2026, 7, 10, 12)


def test_stream_failure_rolls_back_events_cursor_predictions_and_flags(
    ledger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine, factory = ledger
    clock = {"today": date(2026, 7, 8)}
    first_day = date(2026, 7, 7)
    second_day = date(2026, 7, 8)
    sec = _DailySec(
        {
            first_day: [
                _row(
                    accession="first",
                    owner_cik="0000001001",
                    owner_name="Alice",
                    transaction_date=first_day.isoformat(),
                    filed_at=first_day.isoformat(),
                    value_usd=60_000,
                )
            ],
            second_day: [
                _row(
                    accession="second",
                    owner_cik="0000001002",
                    owner_name="Bob",
                    transaction_date=second_day.isoformat(),
                    filed_at=second_day.isoformat(),
                    value_usd=40_000,
                )
            ],
        }
    )
    stream = InsiderClusterStream(
        user_id="ariel",
        config=InsiderClusterConfig(index_publication_lag_days=1),
        sec_adapter=sec,
        market_snapshot=_snapshot,
        today=lambda: clock["today"],
    )
    loop = daily_loop_mod.SignalStreamsDailyLoop(
        streams=[stream],
        session_factory=factory,
        user_id="ariel",
    )
    first_tick = datetime(2026, 7, 8, 12, tzinfo=UTC)
    assert loop._run_sync(first_tick)["streams"]["insider_cluster"]["status"] == "ok"

    original_process = daily_loop_mod.process_nominations

    def fail_after_fetch(*args, **kwargs):
        session = args[0]
        original_process(*args, **kwargs)
        session.add(
            MonitorFlag(
                user_id="ariel",
                kind="signal_stream_warning",
                severity="warning",
                payload="{}",
                surfaced_at=datetime(2026, 7, 9, 12, tzinfo=UTC),
                dedup_key="rollback-probe",
            )
        )
        session.flush()
        raise RuntimeError("pipeline failed after ledger staging")

    monkeypatch.setattr(daily_loop_mod, "process_nominations", fail_after_fetch)
    clock["today"] = date(2026, 7, 9)
    failed = loop._run_sync(datetime(2026, 7, 9, 12, tzinfo=UTC))

    assert failed["streams"]["insider_cluster"]["status"] == "error"
    with factory() as session:
        assert session.query(SignalStreamEvent).count() == 1
        assert session.query(Prediction).count() == 0
        assert session.query(MonitorFlag).count() == 0
        cursor = session.get(
            SignalStreamCursor,
            ("ariel", "insider_cluster"),
        )
        assert cursor is not None
        assert cursor.last_success_at == first_tick.replace(tzinfo=None)
