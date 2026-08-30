from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from argosy.services.earnings_calendar_monitor import (
    EarningsCoverageSummary,
    EarningsEvent,
    run_earnings_calendar_checks,
)
from argosy.services.jobs.earnings_calendar_daily import (
    EarningsCalendarDailyJob,
)
from argosy.services.jobs.news_daily import HoldingsTickers
from argosy.services.news_analyst_runner import _row_to_analyst_input
from argosy.services.stock_decision.fetchers import (
    make_earnings_calendar_fetcher,
)
from argosy.state.models import (
    Base,
    EarningsCoverageReceipt,
    NewsSignal,
    User,
)


def _factory(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'earnings.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id="ariel"))
        session.commit()
    return engine, factory


def test_checks_record_success_empty_error_and_recent_signal(tmp_path) -> None:
    engine, factory = _factory(tmp_path)
    checked_at = datetime(2026, 8, 29, 7, tzinfo=UTC)
    event = EarningsEvent(
        event_at=checked_at - timedelta(days=2),
        eps_estimate=1.0,
        reported_eps=0.8,
        surprise_pct=-20.0,
    )

    def fetcher(ticker: str) -> list[EarningsEvent]:
        if ticker == "AAA":
            return [event]
        if ticker == "BBB":
            return []
        raise RuntimeError("provider unavailable")

    with factory() as session:
        first = run_earnings_calendar_checks(
            session,
            user_id="ariel",
            tickers=["AAA", "BBB", "CCC"],
            checked_at=checked_at,
            fetcher=fetcher,
        )
        session.commit()
    with factory() as session:
        second = run_earnings_calendar_checks(
            session,
            user_id="ariel",
            tickers=["AAA", "BBB", "CCC"],
            checked_at=checked_at,
            fetcher=fetcher,
        )
        session.commit()

    assert first.attempted == 3
    assert first.succeeded == 2
    assert first.empty == 1
    assert first.failures == 1
    assert first.signals_persisted == 1
    assert second.signals_persisted == 0
    assert second.signals_duplicate == 1
    with factory() as session:
        receipts = session.query(EarningsCoverageReceipt).order_by(
            EarningsCoverageReceipt.ticker
        ).all()
        assert [(row.ticker, row.status) for row in receipts] == [
            ("AAA", "ok"),
            ("BBB", "empty"),
            ("CCC", "error"),
        ]
        signal = session.query(NewsSignal).one()
        assert signal.source == "yf_earnings"
        assert _row_to_analyst_input(signal).source == "yf_earnings"
        assert signal.sentiment == "negative"
        assert signal.received_at == event.event_at.replace(tzinfo=None)
        verdict_input = make_earnings_calendar_fetcher(
            session,
            "ariel",
        )("AAA")
        assert verdict_input is not None
        assert "status=ok" in verdict_input
        assert "reported EPS" not in verdict_input
        assert '"reported_eps":0.8' in verdict_input
    engine.dispose()


def test_daily_job_resolves_current_single_stocks_and_commits(tmp_path) -> None:
    engine, factory = _factory(tmp_path)
    seen: list[tuple[str, ...]] = []

    def runner(session: Session, *, tickers, **kwargs):
        seen.append(tuple(tickers))
        return EarningsCoverageSummary(
            attempted=2,
            succeeded=2,
            empty=0,
            failures=0,
            recent_reported_events=1,
            signals_persisted=1,
            signals_duplicate=0,
            errors=(),
        )

    job = EarningsCalendarDailyJob(
        session_factory=factory,
        runner=runner,
        holdings_resolver=lambda session, user_id: HoldingsTickers(
            single_stocks=("NVDA", "TSLA"),
            funds=("CSPX",),
        ),
    )
    result = asyncio.run(job.tick(
        now=lambda: datetime(2026, 8, 29, 7, tzinfo=UTC)
    ))

    assert seen == [("NVDA", "TSLA")]
    assert result["attempted"] == 2
    assert result["failures"] == 0
    assert result["errors"] == []
    engine.dispose()
