from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.inbox.service import build_inbox
from argosy.services.ingest_recommendation_router import route_ingest_recommendations
from argosy.state.models import ActionProposal, Base, ScanState, User


@pytest.fixture
def sync_session(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'ingest-routing.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _recommendation(ticker: str, disposition: str) -> dict[str, str]:
    return {
        "ticker": ticker,
        "disposition": disposition,
        "rationale": f"{ticker} deserves {disposition} follow-up.",
        "next_step": "Verify the thesis with current primary sources.",
        "confidence": "MEDIUM",
    }


def test_watch_enters_argosy_list_without_cluttering_inbox(sync_session) -> None:
    sync_session.add(User(id="ingest-user", plan="free"))
    sync_session.commit()

    routed = route_ingest_recommendations(
        sync_session,
        user_id="ingest-user",
        source_kind="youtube",
        source_id="video-1",
        source_url="https://www.youtube.com/watch?v=video-1",
        recommendations=[_recommendation("TTD", "watch")],
        now=datetime(2026, 9, 11, tzinfo=UTC),
    )

    state = sync_session.get(ScanState, {"user_id": "ingest-user", "ticker": "TTD"})
    proposal = sync_session.get(ActionProposal, routed[0].watchlist_proposal_id)
    assert state is not None and state.status == "active"
    assert state.fleet_json is None and state.last_fleet_at is None
    assert state.estimator_json is None and state.last_estimated_at is None
    assert proposal is not None and proposal.kind == "set_watchlist"
    assert routed[0].inbox_proposal_id is None
    assert all(
        item.id != f"note:{proposal.id}"
        for item in build_inbox(sync_session, user_id="ingest-user").items
    )


def test_buy_enters_list_and_surfaces_review_action_in_inbox(sync_session) -> None:
    sync_session.add(User(id="buy-user", plan="free"))
    sync_session.commit()

    routed = route_ingest_recommendations(
        sync_session,
        user_id="buy-user",
        source_kind="youtube",
        source_id="video-2",
        source_url="https://www.youtube.com/watch?v=video-2",
        recommendations=[_recommendation("APP", "buy")],
        now=datetime(2026, 9, 11, tzinfo=UTC),
    )

    state = sync_session.get(ScanState, {"user_id": "buy-user", "ticker": "APP"})
    proposal_id = routed[0].inbox_proposal_id
    proposal = sync_session.get(ActionProposal, proposal_id)
    inbox_ids = {item.id for item in build_inbox(sync_session, user_id="buy-user").items}
    assert state is not None and state.status == "active"
    assert proposal is not None and proposal.kind == "note_only"
    assert proposal.execution_state == "proposed"
    assert f"note:{proposal_id}" in inbox_ids


def test_rerun_deduplicates_open_recommendations(sync_session) -> None:
    sync_session.add(User(id="dedup-user", plan="free"))
    sync_session.commit()
    kwargs = {
        "user_id": "dedup-user",
        "source_kind": "youtube",
        "source_id": "video-3",
        "source_url": None,
        "recommendations": [_recommendation("BSX", "buy")],
        "now": datetime(2026, 9, 11, tzinfo=UTC),
    }

    first = route_ingest_recommendations(sync_session, **kwargs)
    second = route_ingest_recommendations(sync_session, **kwargs)

    assert first[0].inbox_proposal_id == second[0].inbox_proposal_id
    rows = sync_session.query(ActionProposal).filter_by(user_id="dedup-user").all()
    assert len(rows) == 1


def test_changed_disposition_supersedes_stale_buy_inbox_item(sync_session) -> None:
    sync_session.add(User(id="changed-user", plan="free"))
    sync_session.commit()
    common = {
        "user_id": "changed-user",
        "source_kind": "youtube",
        "source_id": "video-4",
        "source_url": None,
        "now": datetime(2026, 9, 11, tzinfo=UTC),
    }

    buy = route_ingest_recommendations(
        sync_session,
        recommendations=[_recommendation("TTD", "buy")],
        **common,
    )
    watch = route_ingest_recommendations(
        sync_session,
        recommendations=[_recommendation("TTD", "watch")],
        **common,
    )

    stale = sync_session.get(ActionProposal, buy[0].inbox_proposal_id)
    current = sync_session.get(ActionProposal, watch[0].watchlist_proposal_id)
    assert stale is not None and stale.status == "superseded"
    assert stale.execution_state == "dismissed"
    assert current is not None and current.status == "open"


@pytest.mark.parametrize("status", ["active", "quarantined", "dropped"])
def test_new_mention_preserves_review_age_and_quarantine(sync_session, status):
    old = datetime(2026, 8, 1)
    sync_session.add(User(id="existing-user"))
    sync_session.commit()
    state = ScanState(user_id="existing-user", ticker="TTD", status=status,
        quarantine_reason="illiquid" if status == "quarantined" else "",
        fleet_json='{"verdict":"PASS"}', last_fleet_at=old,
        estimator_json='{"go":false}', last_estimated_at=old,
        radar_fingerprint="real-review-fingerprint")
    sync_session.add(state)
    sync_session.commit()
    route_ingest_recommendations(sync_session, user_id="existing-user",
        source_kind="youtube", source_id="new-video", source_url=None,
        recommendations=[_recommendation("TTD", "buy")],
        now=datetime(2026, 9, 11, tzinfo=UTC))
    sync_session.refresh(state)
    assert state.fleet_json == '{"verdict":"PASS"}'
    assert state.last_fleet_at == state.last_estimated_at == old
    assert state.radar_fingerprint == "real-review-fingerprint"
    assert state.status == ("quarantined" if status == "quarantined" else "active")
    assert state.quarantine_reason == ("illiquid" if status == "quarantined" else "")
    assert json.loads(state.nomination_evidence_json)["source_id"] == "new-video"


def test_foreign_fund_lead_uses_usd_fund_listing_not_bare_collision(monkeypatch):
    from types import SimpleNamespace
    from argosy.services.high_potential_funnel import _ingest_candidate
    from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

    calls = []
    async def quote(self, ticker):
        calls.append(ticker)
        return {"price": 6.376, "market_cap": None, "average_volume": 1000,
                "currency": "USD", "quote_type": "ETF", "exchange": "LSE"}
    monkeypatch.setattr(YFinanceAdapter, "get_quote_with_fundamentals", quote)
    candidate = _ingest_candidate(SimpleNamespace(ticker="DPYA", last_score=0),
                                  {"stream": "ingest_youtube", "source_id": "video"})
    assert calls == ["DPYA.L"]
    assert candidate.ticker == "DPYA"
    assert candidate.market_cap is None and candidate.price == 6.376
    assert candidate.evidence["market_facts"]["resolved_ticker"] == "DPYA.L"


@pytest.mark.parametrize("bad_facts", [
    {"price": 6, "currency": "GBP", "quote_type": "ETF"},
    {"price": 6, "currency": "USD", "quote_type": "EQUITY"},
    {"price": float("nan"), "currency": "USD", "quote_type": "ETF"},
])
def test_foreign_fund_listing_mismatch_is_not_accepted(monkeypatch, bad_facts):
    from types import SimpleNamespace
    from argosy.services.high_potential_funnel import _ingest_candidate
    from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

    calls = []
    async def quote(self, ticker):
        calls.append(ticker)
        return bad_facts
    monkeypatch.setattr(YFinanceAdapter, "get_quote_with_fundamentals", quote)
    with pytest.raises(RuntimeError, match="Research lead EXUS"):
        _ingest_candidate(SimpleNamespace(ticker="EXUS", last_score=0),
                          {"stream": "ingest_youtube", "source_id": "video"})
    assert calls and "EXUS" not in calls


def test_ingest_to_real_discovery_loader_with_market_facts(sync_session, monkeypatch):
    from types import SimpleNamespace
    from argosy import config
    from argosy.services import high_potential_funnel as funnel
    from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

    sync_session.add(User(id="loader-user"))
    sync_session.commit()
    route_ingest_recommendations(sync_session, user_id="loader-user",
        source_kind="newsletter", source_id="video", source_url="https://example.org/video",
        recommendations=[_recommendation("TTD", "buy")])
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(
        database_url=str(sync_session.bind.url)))
    async def quote(self, ticker):
        return {"price": 50, "market_cap": 10e9, "average_volume": 1e6,
                "currency": "USD", "quote_type": "EQUITY"}
    monkeypatch.setattr(YFinanceAdapter, "get_quote_with_fundamentals", quote)
    candidates = funnel._load_external_candidates("loader-user")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.ticker == "TTD" and candidate.market_cap == 10e9
    assert candidate.price == 50 and candidate.dollar_volume == 50e6
    assert candidate.event_id == "video"
    assert candidate.evidence["market_facts"]["source"] == "yfinance"
    assert candidate.evidence["market_facts"]["retrieved_at"]
    assert "follow-up" in candidate.reasons[0]


@pytest.mark.parametrize("facts", [
    {"price": None, "market_cap": None, "currency": "USD"},
    {"price": 50, "market_cap": float("nan"), "currency": "USD"},
    {"price": 50, "market_cap": 10e9, "currency": "EUR"},
])
def test_ingest_market_failure_is_not_silently_dropped(monkeypatch, facts):
    from types import SimpleNamespace
    from argosy.services import high_potential_funnel as funnel
    from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

    async def quote(self, ticker):
        return facts
    monkeypatch.setattr(YFinanceAdapter, "get_quote_with_fundamentals", quote)
    monkeypatch.setattr(funnel, "_external_rows", lambda *a: [SimpleNamespace(
        ticker="TTD", last_score=0, nomination_evidence_json=json.dumps({
            "stream": "ingest_youtube", "source_id": "video"}))])
    with pytest.raises(RuntimeError, match="Research lead TTD"):
        funnel._load_external_candidates("loader-user")


def test_fund_nomination_does_not_require_corporate_market_cap(monkeypatch):
    from types import SimpleNamespace
    from argosy.services import high_potential_funnel as funnel
    from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

    async def quote(self, ticker):
        return {"price": 50, "market_cap": None, "currency": "USD", "quote_type": "ETF"}
    monkeypatch.setattr(YFinanceAdapter, "get_quote_with_fundamentals", quote)
    candidate = funnel._ingest_candidate(SimpleNamespace(ticker="SCHD", last_score=0),
        {"stream": "ingest_youtube", "source_id": "video"})
    assert candidate.market_cap is None
    assert "instrument_type=ETF" in candidate.reasons[-1]


@pytest.mark.parametrize("error_type", [ValueError, TypeError])
def test_ingest_provider_exception_is_not_schema_tolerance(monkeypatch, error_type):
    from types import SimpleNamespace
    from argosy.services import high_potential_funnel as funnel
    from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

    async def broken(self, ticker):
        raise error_type("provider decode failed")
    monkeypatch.setattr(YFinanceAdapter, "get_quote_with_fundamentals", broken)
    monkeypatch.setattr(funnel, "_external_rows", lambda *a: [SimpleNamespace(
        ticker="TTD", last_score=0, nomination_evidence_json=json.dumps({
            "stream": "ingest_youtube", "source_id": "video"}))])
    with pytest.raises(error_type, match="provider decode failed"):
        funnel._load_external_candidates("loader-user")


def test_replay_restores_missing_handoff_without_new_time_or_proposal(sync_session):
    from scripts.replay_ingest_leads import replay

    sync_session.add(User(id="replay-user"))
    sync_session.commit()
    original = datetime(2026, 9, 10, 12, tzinfo=UTC)
    route_ingest_recommendations(sync_session, user_id="replay-user",
        source_kind="youtube", source_id="video", source_url=None,
        recommendations=[_recommendation("TTD", "watch")], now=original)
    state = sync_session.get(ScanState, ("replay-user", "TTD"))
    state.nomination_evidence_json = None  # actual legacy broken handoff
    sync_session.commit()
    assert replay(sync_session, user_id="replay-user")["count"] == 1
    assert state.nomination_evidence_json is None
    assert replay(sync_session, user_id="replay-user", apply=True)["count"] == 1
    assert json.loads(state.nomination_evidence_json)["observed_at"] == original.isoformat()
    assert state.last_fleet_at is None and state.fleet_json is None
    assert sync_session.query(ActionProposal).filter_by(user_id="replay-user").count() == 1
    assert replay(sync_session, user_id="replay-user", apply=True)["count"] == 0
