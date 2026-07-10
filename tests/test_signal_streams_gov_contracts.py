"""Stream A government-contract nomination tests."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from http.client import RemoteDisconnected
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import argosy.services.high_potential_funnel as hpf
from argosy.services.contracts import EstimatorVerdict, FleetPick
from argosy.services.predictions.reliability import signal_scorecard_label
from argosy.services.signal_streams.base import SignalNomination, SignalStream
from argosy.services.signal_streams.contracts import (
    AWARD_TYPE_CODES,
    ArgosyMarketSnapshotProvider,
    GovContractsConfig,
    GovContractsStream,
    MarketSnapshot,
    RecipientResolver,
    build_usaspending_payload,
    materiality_ratio,
    parse_usaspending_awards,
    strength_from_materiality,
)
from argosy.services.signal_streams.pipeline import process_nominations
from argosy.services.trend_radar import ScanResult
from argosy.state.models import (
    Base,
    Prediction,
    RecipientResolution,
    ScanState,
    User,
)


class _GoodStream:
    def fetch(self, session, *, since: date) -> list[SignalNomination]:
        return []


def _nomination(**overrides) -> SignalNomination:
    values = {
        "ticker": "PLTR",
        "stream": "gov_contracts",
        "direction": "long",
        "strength": 0.75,
        "as_of": date(2026, 7, 1),
        "evidence": {"award_url": "https://www.usaspending.gov/award/example"},
        "dedup_key": "usaspending:award-1",
    }
    values.update(overrides)
    return SignalNomination(**values)


def _market_nomination(**overrides) -> SignalNomination:
    evidence = {
        "award_url": "https://www.usaspending.gov/award/A1/",
        "revenue_source_url": "https://finance.yahoo.com/quote/PLTR/financials",
        "quote_source_url": "https://finance.yahoo.com/quote/PLTR",
        "price": 25.0,
        "market_cap": 5_000_000_000,
        "average_volume": 10_000_000,
        "obligated_amount": 40_000_000,
    }
    evidence.update(overrides.pop("evidence", {}))
    return _nomination(evidence=evidence, **overrides)


def _snapshot(ticker: str) -> MarketSnapshot:
    return MarketSnapshot(
        price=25.0,
        market_cap=5_000_000_000,
        average_volume=10_000_000,
        trailing_12m_revenue=1_000_000_000,
        revenue_source_url=f"https://finance.yahoo.com/quote/{ticker}/financials",
        quote_source_url=f"https://finance.yahoo.com/quote/{ticker}",
    )


@pytest.fixture
def db_session():
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    session.add(User(id="ariel", plan="free"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_signal_nomination_contract_is_frozen_and_runtime_checkable() -> None:
    nomination = _nomination()
    assert nomination.ticker == "PLTR"
    assert isinstance(_GoodStream(), SignalStream)
    with pytest.raises((AttributeError, TypeError)):
        nomination.strength = 0.5  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ticker", ""),
        ("stream", ""),
        ("direction", "sideways"),
        ("strength", -0.01),
        ("strength", 1.01),
        ("evidence", []),
        ("dedup_key", ""),
    ],
)
def test_signal_nomination_rejects_invalid_contract_fields(field, value) -> None:
    with pytest.raises((TypeError, ValueError)):
        _nomination(**{field: value})


def test_usaspending_payload_requests_prime_contract_obligations() -> None:
    payload = build_usaspending_payload(
        start=date(2026, 4, 3), end=date(2026, 7, 2)
    )
    assert payload["subawards"] is False
    assert payload["filters"]["award_type_codes"] == list(AWARD_TYPE_CODES)
    assert payload["filters"]["time_period"] == [
        {"start_date": "2026-04-03", "end_date": "2026-07-02"}
    ]
    assert "Award Amount" in payload["fields"]
    assert "Base Obligation Date" in payload["fields"]
    assert all("ceiling" not in field.lower() for field in payload["fields"])


def test_usaspending_uses_official_generated_internal_id() -> None:
    payload = {
        "results": [
            {
                "Award ID": "FA8750-26-C-0001",
                "Recipient Name": "Palantir Technologies Inc.",
                "Award Amount": 40_000_000,
                "Base Obligation Date": "2026-07-01",
                "generated_internal_id": "CONT_AWD_OFFICIAL_INTERNAL_ID",
            }
        ]
    }
    request = build_usaspending_payload(
        start=date(2026, 7, 1), end=date(2026, 7, 2)
    )
    awards = parse_usaspending_awards(payload)
    assert "generated_internal_id" in request["fields"]
    assert "generated_unique_award_id" not in request["fields"]
    assert awards[0].stable_id == "CONT_AWD_OFFICIAL_INTERNAL_ID"
    assert awards[0].award_url.endswith(
        "/award/CONT_AWD_OFFICIAL_INTERNAL_ID/"
    )


def test_usaspending_parser_uses_obligation_not_ceiling() -> None:
    payload = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "usaspending_palantir_award_ramp.json"
        ).read_text(encoding="utf-8")
    )
    awards = parse_usaspending_awards(payload)
    assert awards[0].obligated_amount == 40_000_000
    assert awards[0].event_date == date(2026, 7, 1)
    assert awards[0].award_url.endswith(
        "/award/CONT_AWD_FA875026C0001_9700/"
    )
    assert awards[0].obligated_amount != payload["results"][0]["Potential Award Amount"]


def test_recipient_resolution_is_persisted_once_and_unknown_stays_unresolved(
    db_session,
) -> None:
    llm_calls: list[tuple[str, tuple[str, ...]]] = []

    def llm_choice(recipient: str, candidates: dict[str, str]) -> str | None:
        llm_calls.append((recipient, tuple(candidates)))
        return "LMT"

    resolver = RecipientResolver(
        public_companies={"LMT": "Lockheed Martin Corporation"},
        llm_choice=llm_choice,
    )
    assert resolver.resolve(db_session, "Palantir Technologies Inc.") == "PLTR"
    assert resolver.resolve(db_session, "Acme Plumbing LLC") is None
    db_session.commit()
    assert resolver.resolve(db_session, "Acme Plumbing LLC") is None
    rows = db_session.query(RecipientResolution).all()
    assert len(rows) == 2
    assert next(r for r in rows if "acme" in r.recipient_normalized).ticker is None
    assert llm_calls == []


def test_llm_resolver_can_only_choose_from_plausible_public_candidates(
    db_session,
) -> None:
    seen: dict[str, str] = {}

    def llm_choice(recipient: str, candidates: dict[str, str]) -> str | None:
        seen.update(candidates)
        return "NOT_A_CANDIDATE"

    resolver = RecipientResolver(
        public_companies={
            "GD": "General Dynamics Corporation",
            "GDYN": "General Dynamics Software",
            "AAPL": "Apple Inc.",
        },
        llm_choice=llm_choice,
        fuzzy_cutoff=0.45,
        automatic_match_cutoff=0.99,
    )
    assert resolver.resolve(db_session, "General Dynamics Systems") is None
    assert seen and "AAPL" not in seen


def test_materiality_is_revenue_relative_and_strength_is_threshold_normalized() -> None:
    assert materiality_ratio(65_000_000, 1_000_000_000) == pytest.approx(0.065)
    assert strength_from_materiality(0.025, threshold=0.05) == pytest.approx(0.5)
    assert strength_from_materiality(0.10, threshold=0.05) == 1.0


def test_default_market_snapshot_composes_existing_argosy_adapters() -> None:
    gather_calls: list[tuple[list[str], bool]] = []
    quote_calls: list[str] = []

    def gather_fundamentals(tickers, *, with_yfinance_fallback=False):
        gather_calls.append((tickers, with_yfinance_fallback))
        return {
            "PLTR": {
                "revenue_ttm": 1_000_000_000,
                "source_url": "https://finnhub.io/metric?symbol=PLTR",
            }
        }

    class FakeMarketAdapter:
        async def get_quote_with_fundamentals(self, ticker):
            quote_calls.append(ticker)
            return {
                "price": 25.0,
                "market_cap": 5_000_000_000,
                "average_volume": 10_000_000,
            }

    provider = ArgosyMarketSnapshotProvider(
        fundamentals_gatherer=gather_fundamentals,
        market_adapter=FakeMarketAdapter(),
    )
    snapshot = provider("PLTR")
    assert gather_calls == [(["PLTR"], True)]
    assert quote_calls == ["PLTR"]
    assert snapshot.trailing_12m_revenue == 1_000_000_000
    assert snapshot.revenue_source_url.startswith("https://finnhub.io/")
    assert snapshot.price == 25.0
    assert snapshot.market_cap == 5_000_000_000
    assert snapshot.average_volume == 10_000_000
    assert snapshot.quote_source_url == "https://finance.yahoo.com/quote/PLTR"
    assert isinstance(
        GovContractsStream().market_snapshot,
        ArgosyMarketSnapshotProvider,
    )


def test_palantir_fixture_replay_nominates_recent_ramp_with_full_sources(
    db_session,
) -> None:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "usaspending_palantir_award_ramp.json"
        ).read_text(encoding="utf-8")
    )
    requests: list[dict] = []
    stream = GovContractsStream(
        config=GovContractsConfig(
            materiality_threshold=0.05, lookback_days=90
        ),
        fetch_json=lambda payload: requests.append(payload) or fixture,
        resolver=RecipientResolver(),
        market_snapshot=_snapshot,
        today=lambda: date(2026, 7, 2),
    )
    nominations = stream.fetch(db_session, since=date(2026, 7, 1))
    assert len(nominations) == 1
    nomination = nominations[0]
    assert nomination.ticker == "PLTR"
    assert nomination.as_of == date(2026, 7, 1)
    assert nomination.strength == 1.0
    assert nomination.dedup_key == "usaspending:CONT_AWD_FA875026C0001_9700"
    assert nomination.evidence["trailing_90d_obligated"] == 65_000_000
    assert nomination.evidence["materiality_ratio"] == pytest.approx(0.065)
    assert nomination.evidence["award_url"].startswith("https://www.usaspending.gov/award/")
    assert nomination.evidence["revenue_source_url"].endswith("/PLTR/financials")
    assert nomination.evidence["quote_source_url"].endswith("/PLTR")
    assert requests[0]["filters"]["award_type_codes"] == ["A", "B", "C", "D"]


def test_gov_contract_stream_fetches_all_usaspending_pages(db_session) -> None:
    base_row = {
        "Award ID": "A1",
        "Recipient Name": "Palantir Technologies Inc.",
        "Award Amount": 30_000_000,
        "Base Obligation Date": "2026-07-01",
        "generated_internal_id": "AWARD-1",
    }
    pages: list[int] = []

    def fetch_json(payload: dict) -> dict:
        page = payload["page"]
        pages.append(page)
        if page == 1:
            return {
                "results": [base_row],
                "page_metadata": {"hasNext": True},
            }
        return {
            "results": [
                {
                    **base_row,
                    "Award ID": "A2",
                    "generated_internal_id": "AWARD-2",
                }
            ],
            "page_metadata": {"hasNext": False},
        }

    stream = GovContractsStream(
        config=GovContractsConfig(
            materiality_threshold=0.05, lookback_days=90
        ),
        fetch_json=fetch_json,
        resolver=RecipientResolver(),
        market_snapshot=_snapshot,
        curated_contractors={},
        today=lambda: date(2026, 7, 2),
    )
    nominations = stream.fetch(db_session, since=date(2026, 7, 1))
    assert pages == [1, 2, 1, 2]
    assert {nomination.dedup_key for nomination in nominations} == {
        "usaspending:AWARD-1",
        "usaspending:AWARD-2",
    }


def test_global_query_is_recent_and_curated_query_uses_full_history(
    db_session,
) -> None:
    requests: list[dict] = []

    def fetch_json(payload: dict) -> dict:
        requests.append(payload)
        return {"results": [], "page_metadata": {"hasNext": False}}

    stream = GovContractsStream(
        config=GovContractsConfig(
            lookback_days=90,
            recent_scan_days=2,
            max_pages_per_query=3,
        ),
        curated_contractors={"PLTR": "Palantir Technologies Inc"},
        fetch_json=fetch_json,
        today=lambda: date(2026, 7, 10),
    )
    stream.fetch(db_session, since=date(2026, 7, 9))

    global_request = next(
        request
        for request in requests
        if "recipient_search_text" not in request["filters"]
    )
    curated_request = next(
        request
        for request in requests
        if request["filters"].get("recipient_search_text")
        == ["Palantir Technologies Inc"]
    )
    assert global_request["filters"]["time_period"] == [
        {"start_date": "2026-07-09", "end_date": "2026-07-10"}
    ]
    assert curated_request["filters"]["time_period"] == [
        {"start_date": "2026-04-12", "end_date": "2026-07-10"}
    ]


def test_global_and_curated_duplicate_award_is_merged_once(db_session) -> None:
    duplicate = {
        "Award ID": "DUP",
        "Recipient Name": "Palantir Technologies Inc.",
        "Award Amount": 60_000_000,
        "Base Obligation Date": "2026-07-10",
        "generated_internal_id": "DUP-STABLE",
    }

    stream = GovContractsStream(
        config=GovContractsConfig(
            materiality_threshold=0.05,
            lookback_days=90,
            recent_scan_days=2,
            max_pages_per_query=3,
        ),
        curated_contractors={"PLTR": "Palantir Technologies Inc"},
        fetch_json=lambda payload: {
            "results": [duplicate],
            "page_metadata": {"hasNext": False},
        },
        resolver=RecipientResolver(),
        market_snapshot=_snapshot,
        today=lambda: date(2026, 7, 10),
    )

    nominations = stream.fetch(db_session, since=date(2026, 4, 12))

    assert len(nominations) == 1
    assert nominations[0].dedup_key == "usaspending:DUP-STABLE"
    assert nominations[0].evidence["trailing_90d_obligated"] == 60_000_000


def test_newly_resolved_global_recipient_gets_full_history_query(
    db_session,
) -> None:
    requests: list[dict] = []
    recent = {
        "Award ID": "NEW-RECENT",
        "Recipient Name": "Novel Defense Corporation",
        "Award Amount": 10_000_000,
        "Base Obligation Date": "2026-07-10",
        "generated_internal_id": "NEW-RECENT",
    }
    older = {
        "Award ID": "NEW-OLDER",
        "Recipient Name": "Novel Defense Corporation",
        "Award Amount": 50_000_000,
        "Base Obligation Date": "2026-06-01",
        "generated_internal_id": "NEW-OLDER",
    }

    def fetch_json(payload: dict) -> dict:
        requests.append(payload)
        recipient = payload["filters"].get("recipient_search_text")
        return {
            "results": [recent, older] if recipient else [recent],
            "page_metadata": {"hasNext": False},
        }

    stream = GovContractsStream(
        config=GovContractsConfig(
            materiality_threshold=0.05,
            lookback_days=90,
            recent_scan_days=2,
            max_pages_per_query=3,
        ),
        curated_contractors={},
        fetch_json=fetch_json,
        resolver=RecipientResolver(
            public_companies={"NEW": "Novel Defense Corporation"}
        ),
        market_snapshot=_snapshot,
        today=lambda: date(2026, 7, 10),
    )

    nominations = stream.fetch(db_session, since=date(2026, 7, 9))

    assert any(
        request["filters"].get("recipient_search_text")
        == ["Novel Defense Corporation"]
        and request["filters"]["time_period"][0]["start_date"] == "2026-04-12"
        for request in requests
    )
    assert len(nominations) == 1
    assert nominations[0].ticker == "NEW"
    assert nominations[0].evidence["trailing_90d_obligated"] == 60_000_000


def test_usaspending_page_cap_exhaustion_fails_without_partial_results(
    db_session,
) -> None:
    pages: list[int] = []

    def fetch_json(payload: dict) -> dict:
        pages.append(payload["page"])
        return {
            "results": [],
            "page_metadata": {"hasNext": True},
        }

    stream = GovContractsStream(
        config=GovContractsConfig(
            recent_scan_days=2,
            max_pages_per_query=2,
        ),
        curated_contractors={},
        fetch_json=fetch_json,
        today=lambda: date(2026, 7, 10),
    )

    with pytest.raises(RuntimeError, match="page cap"):
        stream.fetch(db_session, since=date(2026, 7, 9))

    assert pages == [1, 2]
    assert db_session.query(RecipientResolution).count() == 0


@pytest.mark.parametrize(
    "transient_error",
    [
        TimeoutError("read timed out"),
        URLError("connection reset while reading"),
        RemoteDisconnected("remote closed"),
    ],
)
def test_usaspending_page_retry_includes_success_once_and_continues(
    db_session, transient_error
) -> None:
    calls: list[int] = []
    sleeps: list[float] = []
    page_2_attempts = 0

    def award(stable_id: str) -> dict:
        return {
            "Award ID": stable_id,
            "Recipient Name": "Palantir Technologies Inc.",
            "Award Amount": 30_000_000,
            "Base Obligation Date": "2026-07-01",
            "generated_internal_id": stable_id,
        }

    def fetch_json(payload: dict) -> dict:
        nonlocal page_2_attempts
        page = payload["page"]
        calls.append(page)
        if page == 1:
            return {
                "results": [award("AWARD-1")],
                "page_metadata": {"hasNext": True},
            }
        page_2_attempts += 1
        if page_2_attempts == 1:
            raise transient_error
        return {
            "results": [award("AWARD-2")],
            "page_metadata": {"hasNext": False},
        }

    stream = GovContractsStream(
        config=GovContractsConfig(
            materiality_threshold=0.05, lookback_days=90
        ),
        fetch_json=fetch_json,
        resolver=RecipientResolver(),
        market_snapshot=_snapshot,
        curated_contractors={},
        today=lambda: date(2026, 7, 2),
        max_page_attempts=3,
        page_retry_backoff=(0.25, 0.5),
        sleep=sleeps.append,
    )
    nominations = stream.fetch(db_session, since=date(2026, 7, 1))
    assert calls == [1, 2, 2, 1, 2]
    assert sleeps == [0.25]
    assert [nomination.dedup_key for nomination in nominations] == [
        "usaspending:AWARD-1",
        "usaspending:AWARD-2",
    ]


def test_usaspending_exhausted_page_retry_fails_without_partial_results(
    db_session,
) -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def fetch_json(payload: dict) -> dict:
        page = payload["page"]
        calls.append(page)
        if page == 1:
            return {
                "results": [
                    {
                        "Award ID": "AWARD-1",
                        "Recipient Name": "Palantir Technologies Inc.",
                        "Award Amount": 30_000_000,
                        "Base Obligation Date": "2026-07-01",
                        "generated_internal_id": "AWARD-1",
                    }
                ],
                "page_metadata": {"hasNext": True},
            }
        raise TimeoutError("page 2 timed out")

    stream = GovContractsStream(
        fetch_json=fetch_json,
        curated_contractors={},
        max_page_attempts=2,
        page_retry_backoff=(0.1,),
        sleep=sleeps.append,
        today=lambda: date(2026, 7, 2),
    )
    with pytest.raises(TimeoutError, match="page 2 timed out"):
        stream.fetch(db_session, since=date(2026, 7, 1))
    assert calls == [1, 2, 2]
    assert sleeps == [0.1]
    assert db_session.query(RecipientResolution).count() == 0


def test_usaspending_does_not_retry_programming_errors(db_session) -> None:
    calls = 0
    sleeps: list[float] = []

    def fetch_json(payload: dict) -> dict:
        nonlocal calls
        calls += 1
        raise ValueError("response parser bug")

    stream = GovContractsStream(
        fetch_json=fetch_json,
        curated_contractors={},
        max_page_attempts=3,
        page_retry_backoff=(0.1, 0.2),
        sleep=sleeps.append,
        today=lambda: date(2026, 7, 2),
    )
    with pytest.raises(ValueError, match="response parser bug"):
        stream.fetch(db_session, since=date(2026, 7, 1))
    assert calls == 1
    assert sleeps == []


def test_process_nominations_anchors_prediction_windows_to_observed_at(
    db_session,
) -> None:
    nomination = _market_nomination(as_of=date(2026, 6, 1))
    observed_at = datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    process_nominations(
        db_session,
        user_id="ariel",
        nominations=[nomination],
        observed_at=observed_at,
    )
    db_session.commit()
    rows = db_session.query(Prediction).order_by(Prediction.timeframe_days).all()
    observed_naive = observed_at.replace(tzinfo=None)
    assert [row.event_at for row in rows] == [observed_naive, observed_naive]
    assert [row.evaluation_due_at for row in rows] == [
        observed_naive + timedelta(days=30),
        observed_naive + timedelta(days=180),
    ]
    state = db_session.get(ScanState, ("ariel", "PLTR"))
    evidence = json.loads(state.nomination_evidence_json)
    assert evidence["as_of"] == "2026-06-01"


def test_nomination_writes_auditable_radar_state_and_true_30_180_predictions(
    db_session,
) -> None:
    nomination = _market_nomination()
    observed_at = datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    summary = process_nominations(
        db_session,
        user_id="ariel",
        nominations=[nomination],
        observed_at=observed_at,
    )
    db_session.commit()
    state = db_session.get(ScanState, ("ariel", "PLTR"))
    evidence = json.loads(state.nomination_evidence_json)
    assert state.status == "active"
    assert evidence["stream"] == "gov_contracts"
    assert evidence["dedup_key"] == nomination.dedup_key
    assert "stream=gov_contracts" in state.radar_fingerprint
    assert f"event={nomination.dedup_key}" in state.radar_fingerprint
    assert summary.active == 1
    rows = db_session.query(Prediction).order_by(Prediction.timeframe_days).all()
    assert [row.timeframe_days for row in rows] == [30, 180]
    assert [row.evaluation_method for row in rows] == [
        "fixed_lookahead_30d",
        "fixed_lookahead_180d",
    ]
    assert all(float(row.entry_price) == 25.0 for row in rows)
    event_at = observed_at.replace(tzinfo=None)
    assert rows[0].evaluation_due_at == event_at + timedelta(days=30)
    assert rows[1].evaluation_due_at == event_at + timedelta(days=180)
    assert all(row.source == "signal_stream:gov_contracts" for row in rows)
    original = [(row.id, row.entry_price, row.created_at) for row in rows]
    process_nominations(
        db_session,
        user_id="ariel",
        nominations=[nomination],
        observed_at=observed_at + timedelta(hours=1),
    )
    db_session.commit()
    rerun = db_session.query(Prediction).order_by(Prediction.timeframe_days).all()
    assert [(row.id, row.entry_price, row.created_at) for row in rerun] == original


def test_180d_prediction_scores_at_the_real_180_day_due_date(
    db_session,
) -> None:
    from argosy.services.predictions.evaluator import Bar, evaluate_prediction

    nomination = _market_nomination()
    observed_at = datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    process_nominations(
        db_session,
        user_id="ariel",
        nominations=[nomination],
        observed_at=observed_at,
    )
    prediction = db_session.query(Prediction).filter(
        Prediction.timeframe_days == 180
    ).one()
    seen: dict[str, date] = {}

    def fetcher(ticker: str, start: date, end: date):
        seen["end"] = end
        return [
            Bar(
                bar_date=end,
                open=29.0,
                high=31.0,
                low=28.0,
                close=30.0,
            )
        ]

    outcome = evaluate_prediction(
        db_session, prediction, price_fetcher=fetcher
    )
    assert seen["end"] == observed_at.date() + timedelta(days=180)
    assert outcome.outcome_kind == "hit_target"
    assert float(outcome.pnl_pct) == pytest.approx(0.2)


def test_external_nomination_keeps_liquidity_gate_but_bypasses_pump_guard(
    db_session,
) -> None:
    illiquid = _market_nomination(
        ticker="PENNY",
        evidence={
            **_market_nomination().evidence,
            "price": 1.0,
            "quote_source_url": "https://finance.yahoo.com/quote/PENNY",
        },
        dedup_key="usaspending:penny-award",
    )
    summary = process_nominations(
        db_session, user_id="ariel", nominations=[illiquid]
    )
    db_session.commit()
    state = db_session.get(ScanState, ("ariel", "PENNY"))
    assert state.status == "quarantined"
    assert state.quarantine_reason == "failed-liquidity"
    assert summary.quarantined == 1
    assert db_session.query(Prediction).count() == 2


def test_external_nomination_reaches_existing_estimator_and_fleet_path(
    monkeypatch,
) -> None:
    nomination = _market_nomination()
    captured_states: list[dict] = []
    estimate_calls: list[str] = []
    grade_calls: list[str] = []
    monkeypatch.setattr(
        hpf,
        "_scan_radar",
        lambda: ScanResult(shortlist=(), quarantine=(), source_counts={}),
    )
    monkeypatch.setattr(
        hpf,
        "_load_external_candidates",
        lambda user_id: process_nominations(
            None, user_id=user_id, nominations=[nomination], persist=False
        ).candidates,
    )
    monkeypatch.setattr(hpf, "_load_external_quarantine", lambda user_id: [])
    monkeypatch.setattr(hpf, "_load_scan_states", lambda user_id: {})
    monkeypatch.setattr(
        hpf,
        "_persist_scan_states",
        lambda user_id, states: captured_states.extend(states),
    )

    def estimate(candidate, *, user_id):
        estimate_calls.append(candidate.ticker)
        return EstimatorVerdict(
            ticker=candidate.ticker,
            go=True,
            conviction="HIGH",
            sentiment=0.9,
            one_line="material award ramp",
        )

    async def grade(user_id, candidate, **kwargs):
        grade_calls.append(candidate.ticker)
        return FleetPick(candidate.ticker, "HIGH", "thesis", "BUY", ())

    monkeypatch.setattr(hpf, "_estimate", estimate)
    monkeypatch.setattr(hpf, "_grade", grade)
    result = asyncio.run(
        hpf.run_funnel(
            "ariel",
            now=datetime(2026, 7, 2, tzinfo=UTC),
        )
    )
    assert estimate_calls == ["PLTR"]
    assert grade_calls == ["PLTR"]
    assert [pick.ticker for pick in result.picks] == ["PLTR"]
    assert json.loads(captured_states[0]["nomination_evidence_json"])[
        "dedup_key"
    ] == nomination.dedup_key


def test_signal_scorecard_label_is_explicitly_uncalibrated_below_30() -> None:
    assert signal_scorecard_label(scored=7, observation_days=42) == (
        "uncalibrated (beta — 7 scored over 42 days)"
    )
    assert signal_scorecard_label(scored=30, observation_days=100) == "calibrated"


def test_discovery_candidate_and_stage2_prompt_carry_exact_signal_scorecard(
    db_session, monkeypatch
) -> None:
    import argosy.services.decision_funnel.discovery_candidates as dc
    from argosy.services.decision_funnel.triage import Stage2TriageAgent
    from argosy.services.high_potential_funnel import _pick_to_json

    scorecard = {
        "source": "signal_stream:gov_contracts",
        "win_rate": 0.6,
        "scored_outcomes": 12,
        "avg_pnl_pct": 0.08,
        "observation_days": 77,
        "calibration": "uncalibrated (beta — 12 scored over 77 days)",
    }
    monkeypatch.setattr(
        dc,
        "signal_source_scorecard",
        lambda session, user_id, stream: dict(scorecard),
    )
    db_session.add(
        ScanState(
            user_id="ariel",
            ticker="PLTR",
            status="active",
            fleet_json=_pick_to_json(
                FleetPick("PLTR", "HIGH", "thesis", "BUY", ())
            ),
            nomination_evidence_json=json.dumps(
                {
                    "stream": "gov_contracts",
                    "dedup_key": "usaspending:A1",
                    "evidence": {"award_url": "https://example.test/A1"},
                }
            ),
        )
    )
    db_session.commit()
    candidates = dc.load_discovery_candidates(
        db_session, user_id="ariel", held_tickers=set()
    )
    candidate = candidates[0]
    assert candidate.extra["signal_stream"] == "gov_contracts"
    assert candidate.extra["signal_scorecard"] == scorecard
    _, prompt = Stage2TriageAgent(user_id="ariel").build_prompt(
        candidate=candidate,
        market_summary="neutral",
        weight_pct=None,
        cap_pct=None,
    )
    assert "SIGNAL STREAM: gov_contracts" in prompt
    assert json.dumps(scorecard, sort_keys=True) in prompt


def test_predictions_loop_runs_reevaluation_before_retention_in_same_session(
    monkeypatch,
) -> None:
    import argosy.orchestrator.loops.predictions_evaluator as loop_mod
    from argosy.services.predictions.evaluator import (
        EvaluatorSummary,
        ReevaluationSummary,
    )
    from argosy.services.predictions.retention import RetentionSummary

    engine = sa.create_engine("sqlite://")
    factory = sessionmaker(bind=engine)
    calls: list[tuple[str, object]] = []

    def evaluator(session, **kwargs):
        calls.append(("evaluator", session))
        return EvaluatorSummary()

    def reevaluator(session, **kwargs):
        calls.append(("reevaluator", session))
        return ReevaluationSummary(candidates=1, reevaluated=1)

    def retention(session, **kwargs):
        calls.append(("retention", session))
        return RetentionSummary()

    monkeypatch.setattr(loop_mod, "run_evaluator_batch", evaluator)
    monkeypatch.setattr(loop_mod, "run_reevaluation_batch", reevaluator)
    monkeypatch.setattr(loop_mod, "run_retention_pass", retention)
    loop = loop_mod.PredictionsEvaluatorLoop(session_factory=factory)
    summary = loop._run_tick_sync(datetime(2026, 7, 2, tzinfo=UTC))
    assert [name for name, _ in calls] == [
        "evaluator",
        "reevaluator",
        "retention",
    ]
    assert len({id(session) for _, session in calls}) == 1
    assert summary["reevaluation"]["reevaluated"] == 1


def test_signal_streams_daily_is_cron_registered_and_isolates_stream_failures(
    tmp_path,
) -> None:
    from argosy.orchestrator.loops.signal_streams_daily import (
        SignalStreamsDailyLoop,
        signal_streams_daily_metadata,
    )

    class BrokenStream:
        name = "broken"

        def fetch(self, session, *, since):
            raise RuntimeError("source unavailable")

    class GoodStream:
        name = "gov_contracts"

        def fetch(self, session, *, since):
            return [_market_nomination()]

    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'signals.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as setup:
        setup.add(User(id="ariel", plan="free"))
        setup.commit()
    loop = SignalStreamsDailyLoop(
        streams=[BrokenStream(), GoodStream()],
        session_factory=factory,
        user_id="ariel",
    )
    summary = asyncio.run(
        loop.tick(
            now=lambda: datetime(
                2026, 7, 2, 12, 0, tzinfo=UTC
            )
        )
    )
    assert loop.schedule.cron == "30 15 * * *"
    assert summary["streams"]["broken"]["status"] == "error"
    assert summary["streams"]["gov_contracts"]["status"] == "ok"
    assert summary["streams"]["gov_contracts"]["nominations"] == 1
    with factory() as verify:
        assert verify.query(Prediction).count() == 2
    metadata = signal_streams_daily_metadata()
    assert metadata.name == "signal_streams_daily"
    assert metadata.schedule_cron == "30 15 * * *"
    assert metadata.source_kind == "ingest"
    engine.dispose()


def test_signal_stream_loop_bootstraps_then_uses_recent_since(
    tmp_path,
) -> None:
    from argosy.orchestrator.loops.signal_streams_daily import (
        SignalStreamsDailyLoop,
    )

    class RecordingStream:
        name = "gov_contracts"
        config = SimpleNamespace(lookback_days=90, recent_scan_days=2)

        def __init__(self) -> None:
            self.since: list[date] = []

        def fetch(self, session, *, since):
            self.since.append(since)
            return []

    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'bootstrap.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as setup:
        setup.add(User(id="ariel", plan="free"))
        setup.commit()
    stream = RecordingStream()
    loop = SignalStreamsDailyLoop(
        streams=[stream],
        session_factory=factory,
        user_id="ariel",
    )
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

    loop._run_sync(now)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO predictions "
                "(user_id, source, source_ref, ticker, direction, "
                "entry_price, timeframe_days, message_id, event_at, "
                "evaluation_due_at, evaluation_method) VALUES "
                "('ariel', 'signal_stream:gov_contracts', '{}', 'PLTR', "
                "'long', 25, 30, 'prior-signal', '2026-07-01', "
                "'2026-07-31', 'fixed_lookahead_30d')"
            )
        )
    loop._run_sync(now)

    assert stream.since == [date(2026, 4, 12), date(2026, 7, 9)]
    engine.dispose()


def test_default_scheduler_registers_signal_streams_before_discovery() -> None:
    from argosy.agent_settings import AgentSettings
    from argosy.orchestrator.scheduler import Scheduler

    scheduler = Scheduler(settings=AgentSettings())
    scheduler.register_default_loops()
    signal_loop = scheduler._loops["signal_streams_daily"]
    discovery_loop = scheduler._loops["discovery_funnel"]
    assert signal_loop.schedule.cron == "30 15 * * *"
    assert discovery_loop.schedule.cron == "0 16 * * *"


def test_stream_a_migration_adds_resolution_evidence_and_180d_methods(
    tmp_path, monkeypatch
) -> None:
    from alembic.config import Config

    from alembic import command

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    (tmp_path / "db").mkdir()
    reload_settings()
    command.upgrade(Config("alembic.ini"), "head")
    sync_url = get_settings().database_url.replace("+aiosqlite", "")
    engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False}
    )
    inspector = sa.inspect(engine)
    assert inspector.has_table("signal_recipient_resolutions")
    scan_columns = {
        column["name"]
        for column in inspector.get_columns("trend_scan_state")
    }
    assert "nomination_evidence_json" in scan_columns
    with engine.begin() as connection:
        methods = connection.execute(
            sa.text(
                "SELECT method_name, method_version "
                "FROM evaluation_method_registry "
                "WHERE method_name LIKE 'fixed_lookahead_180d%' "
                "ORDER BY method_version"
            )
        ).all()
        assert [tuple(row) for row in methods] == [
            ("fixed_lookahead_180d", 1),
            ("fixed_lookahead_180d_entry_backfilled", 2),
        ]
        connection.execute(
            sa.text(
                "INSERT INTO users (id, plan) VALUES "
                "('migration-test', 'free')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO predictions "
                "(user_id, source, source_ref, ticker, direction, "
                "entry_price, timeframe_days, message_id, event_at, "
                "evaluation_due_at, evaluation_method) VALUES "
                "('migration-test', 'signal_stream:gov_contracts', '{}', "
                "'PLTR', 'long', 25, 180, 'migration-signal', "
                "'2026-01-01', '2026-06-30', 'fixed_lookahead_180d')"
            )
        )
    engine.dispose()


def test_stream_a_migration_round_trips_on_empty_database(
    tmp_path, monkeypatch
) -> None:
    from alembic.config import Config

    from alembic import command

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings

    (tmp_path / "db").mkdir()
    reload_settings()
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.downgrade(config, "-1")
    command.upgrade(config, "head")
