from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from argosy.agents.stock_decision import (
    StockDecisionAgent,
    bundle_has_sufficient_evidence,
)
from argosy.services.jobs.news_daily import HoldingsTickers
from argosy.services.jobs.sec_earnings_daily import SecEarningsDailyJob
from argosy.services.news_analyst_runner import _row_to_analyst_input
from argosy.services.sec_earnings_monitor import (
    SecEarningsCoverageSummary,
    SecEarningsFetcher,
    SecEarningsFiling,
    run_sec_earnings_checks,
)
from argosy.services.stock_decision.fetchers import make_earnings_filing_fetcher
from argosy.state.models import (
    Base,
    EarningsCoverageReceipt,
    NewsSignal,
    User,
)


def _factory(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'sec-earnings.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id="ariel"))
        session.commit()
    return engine, factory


def _filing(ticker: str, checked_at: datetime) -> SecEarningsFiling:
    return SecEarningsFiling(
        ticker=ticker,
        cik="0000000123",
        accession="0000000123-26-000001",
        form="8-K",
        filed_at=checked_at,
        report_date="2026-08-27",
        items="2.02,9.01",
        source_url=f"https://www.sec.gov/Archives/{ticker}-filing",
        evidence_excerpt=(
            f"{ticker} filed primary SEC 8-K. Revenue decreased 20 percent "
            "and management cut guidance."
        ),
        raw_text="Primary filing full text; revenue decreased and guidance was cut.",
    )


def test_sec_ticker_alias_resolves_argosy_slash_symbol() -> None:
    class _Client:
        def get(self, url):
            class _Response:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {
                        "0": {"ticker": "BRK-B", "cik_str": 1067983},
                    }

            return _Response()

    fetcher = SecEarningsFetcher(client=_Client())
    assert fetcher._cik_for("BRK/B") == "0001067983"


def test_sec_checks_persist_receipts_signal_and_true_source(tmp_path) -> None:
    engine, factory = _factory(tmp_path)
    checked_at = datetime(2026, 8, 29, 8, tzinfo=UTC)

    def fetcher(ticker: str, *, cutoff):
        if ticker == "AAA":
            return [_filing(ticker, checked_at)]
        if ticker == "BBB":
            return []
        raise RuntimeError("SEC unavailable")

    with factory() as session:
        first = run_sec_earnings_checks(
            session,
            user_id="ariel",
            tickers=["AAA", "BBB", "CCC"],
            checked_at=checked_at,
            fetcher=fetcher,
        )
        session.commit()
    with factory() as session:
        second = run_sec_earnings_checks(
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
    assert second.signals_duplicate == 1
    with factory() as session:
        receipts = session.query(EarningsCoverageReceipt).order_by(
            EarningsCoverageReceipt.ticker
        ).all()
        assert [(row.ticker, row.provider, row.status) for row in receipts] == [
            ("AAA", "sec_edgar", "ok"),
            ("BBB", "sec_edgar", "empty"),
            ("CCC", "sec_edgar", "error"),
        ]
        signal = session.query(NewsSignal).one()
        assert signal.source == "sec_filing"
        assert signal.source_trust == "high"
        assert _row_to_analyst_input(signal).source == "sec_filing"
        evidence = make_earnings_filing_fetcher(session, "ariel")("AAA")
        assert evidence is not None
        assert evidence.startswith("source=sec_edgar_primary_filing;")
        assert "Revenue decreased" in evidence
    engine.dispose()


def test_historical_primary_filing_informs_review_without_fake_fresh_signal(tmp_path) -> None:
    engine, factory = _factory(tmp_path)
    checked_at = datetime(2026, 8, 29, 8, tzinfo=UTC)
    old_filing = _filing("AAA", datetime(2026, 5, 20, 8, tzinfo=UTC))
    seen_cutoffs = []

    def fetcher(ticker: str, *, cutoff):
        seen_cutoffs.append(cutoff)
        return [old_filing]

    with factory() as session:
        summary = run_sec_earnings_checks(
            session,
            user_id="ariel",
            tickers=["AAA"],
            checked_at=checked_at,
            fetcher=fetcher,
        )
        session.commit()
        receipt = session.query(EarningsCoverageReceipt).one()
        assert receipt.status == "ok"
        assert receipt.latest_reported_at.date().isoformat() == "2026-05-20"
        assert session.query(NewsSignal).count() == 0

    assert seen_cutoffs == [datetime(2025, 2, 25, tzinfo=UTC).date()]
    assert summary.recent_filings == 0
    assert summary.signals_persisted == 0
    engine.dispose()


def test_same_day_empty_retry_cannot_erase_good_filing_packet(tmp_path) -> None:
    engine, factory = _factory(tmp_path)
    checked_at = datetime(2026, 8, 29, 8, tzinfo=UTC)

    with factory() as session:
        run_sec_earnings_checks(
            session,
            user_id="ariel",
            tickers=["AAA"],
            checked_at=checked_at,
            fetcher=lambda ticker, cutoff: [_filing(ticker, checked_at)],
        )
        session.commit()
    with factory() as session:
        retry = run_sec_earnings_checks(
            session,
            user_id="ariel",
            tickers=["AAA"],
            checked_at=checked_at.replace(hour=10),
            fetcher=lambda ticker, cutoff: [],
        )
        session.commit()
        receipt = session.query(EarningsCoverageReceipt).one()
        assert retry.empty == 1
        assert receipt.status == "ok"
        assert receipt.checked_at == checked_at.replace(tzinfo=None)
        assert "AAA-filing" in receipt.events_json
    engine.dispose()


def test_same_day_error_retry_cannot_erase_successful_check(tmp_path) -> None:
    engine, factory = _factory(tmp_path)
    checked_at = datetime(2026, 8, 29, 8, tzinfo=UTC)

    with factory() as session:
        run_sec_earnings_checks(
            session,
            user_id="ariel",
            tickers=["AAA"],
            checked_at=checked_at,
            fetcher=lambda ticker, cutoff: [_filing(ticker, checked_at)],
        )
        session.commit()

    def outage(ticker, *, cutoff):
        raise RuntimeError("SEC unavailable")

    with factory() as session:
        retry = run_sec_earnings_checks(
            session,
            user_id="ariel",
            tickers=["AAA"],
            checked_at=checked_at.replace(hour=10),
            fetcher=outage,
        )
        session.commit()
        receipt = session.query(EarningsCoverageReceipt).one()
        assert retry.failures == 1
        assert retry.errors == ({"ticker": "AAA", "error": "SEC unavailable"},)
        assert receipt.status == "ok"
        assert receipt.checked_at == checked_at.replace(tzinfo=None)
        assert receipt.error_message is None
        assert "AAA-filing" in receipt.events_json
    engine.dispose()


def test_stock_decision_prompt_actually_contains_earnings_evidence() -> None:
    bundle = {
        "earnings_calendar": "source=yfinance; reported_eps=0.8",
        "earnings_filing": "source=sec; management cut guidance",
        "tax": "source=authoritative_tax_engine; exact sale can be resolved",
    }
    assert bundle_has_sufficient_evidence(bundle)
    _system, prompt = StockDecisionAgent(user_id="ariel").build_prompt(
        ticker="AAA",
        context="held $10,000",
        bundle=bundle,
    )
    assert "reported_eps=0.8" in prompt
    assert "management cut guidance" in prompt
    assert "authoritative_tax_engine" in prompt
    assert "below roughly 1% of the whole book" in _system


def test_sec_daily_job_resolves_current_single_stocks(tmp_path) -> None:
    engine, factory = _factory(tmp_path)
    seen: list[tuple[str, ...]] = []

    def runner(session: Session, *, tickers, **kwargs):
        seen.append(tuple(tickers))
        return SecEarningsCoverageSummary(
            attempted=2,
            succeeded=2,
            empty=1,
            failures=0,
            recent_filings=1,
            signals_persisted=1,
            signals_duplicate=0,
            errors=(),
        )

    job = SecEarningsDailyJob(
        session_factory=factory,
        runner=runner,
        holdings_resolver=lambda session, user_id: HoldingsTickers(
            single_stocks=("NVDA", "TSLA"),
            funds=("CSPX",),
        ),
    )
    result = asyncio.run(job.tick(
        now=lambda: datetime(2026, 8, 29, 8, tzinfo=UTC)
    ))
    assert seen == [("NVDA", "TSLA")]
    assert result["recent_filings"] == 1
    engine.dispose()
