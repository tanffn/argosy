"""Tests for the funnel book loader + Stage-0 market read (DB-backed)."""
import json

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.decision_funnel.book import load_book
from argosy.services.decision_funnel.stage0_market import build_market_read
from argosy.state.models import (
    AlphaReportAnalysis,
    Base,
    NewsSignal,
    PortfolioSnapshotRow,
    User,
)


@pytest.fixture
def session():
    eng = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng, expire_on_commit=False)
    s = SF()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


def _add_snapshot(session, positions):
    from datetime import date, datetime, timezone

    session.add(
        PortfolioSnapshotRow(
            user_id="ariel",
            snapshot_date=date(2026, 6, 22),
            imported_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
            positions_json=json.dumps(positions),
        )
    )
    session.commit()


def test_load_book_weights_exclude_cash_and_real_estate(session):
    _add_snapshot(session, [
        {"symbol": "NVDA", "asset_type": "Individual Stocks", "usd_value_k": 600},
        {"symbol": "CSPX", "asset_type": "Core Equity", "usd_value_k": 300},
        {"symbol": "-", "asset_type": "Cash", "usd_value_k": 100},
        {"symbol": "", "asset_type": "Real estate", "usd_value_k": 5000},
    ])
    book = load_book(session, user_id="ariel")
    syms = {h.ticker for h in book}
    assert syms == {"NVDA", "CSPX"}  # cash + real estate excluded
    nvda = next(h for h in book if h.ticker == "NVDA")
    # 600 / (600+300) = 66.67%
    assert round(nvda.weight_pct, 1) == 66.7


def test_load_book_empty_when_no_snapshot(session):
    assert load_book(session, user_id="ariel") == []


def test_market_read_neutral_when_no_signals(session):
    read = build_market_read(session, user_id="ariel")
    assert read.risk_off is False
    assert read.macro_tone is None
    assert read.summary == "no material macro signal"


def test_market_read_risk_off_on_bearish_tone(session):
    from datetime import datetime, timezone

    ns = NewsSignal(
        source="alpha_report", source_ref="ar-1",
        received_at=datetime.now(timezone.utc), sentiment="bearish",
        source_trust="high", evidence_excerpt="x", raw_text="x",
    )
    session.add(ns)
    session.commit()
    session.add(
        AlphaReportAnalysis(
            news_signal_id=ns.id,
            user_id="ariel",
            analyzed_at=datetime(2026, 6, 22, 18, tzinfo=timezone.utc),
            macro_tone="bearish",
            macro_tone_confidence="high",
            key_themes=json.dumps(["AI cycle", "rate cuts"]),
            summary_rationale="x",
            ticker_signals_json=json.dumps([{"ticker": "NVDA", "sentiment": "bearish"}]),
            structural_picks_json=json.dumps([]),
            cautions_json=json.dumps([]),
            index_targets_json=json.dumps({}),
            confidence_overall="high",
            agent_version="v1",
        )
    )
    session.commit()
    read = build_market_read(session, user_id="ariel")
    assert read.risk_off is True
    assert read.macro_tone == "bearish"
    assert "AI cycle" in read.key_themes


def test_market_read_collects_high_materiality_news(session):
    from datetime import datetime, timezone

    session.add(
        NewsSignal(
            source="finnhub",
            source_ref="abc-1",
            received_at=datetime.now(timezone.utc),
            parsed_tickers=json.dumps(["NVDA"]),
            sentiment="bearish",
            source_trust="high",
            evidence_excerpt="guidance cut",
            raw_text="full text",
            materiality="high",
        )
    )
    session.commit()
    read = build_market_read(session, user_id="ariel")
    hits = {n.ticker for n in read.high_materiality_news}
    assert "NVDA" in hits
