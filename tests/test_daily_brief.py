"""Investor-event gather tests for the (retired) DailyBriefLoop.

W9 — the Phase 2 ``DailyBriefLoop`` four-agent orchestration was retired
in favour of T4.5's ``argosy/services/daily_brief_runner.py`` (single
agent, simpler, also persists ``daily_briefs``). The
``test_daily_brief_end_to_end`` test that exercised the old orchestration
is gone; runner end-to-end coverage lives in
``tests/test_daily_brief_runner.py``.

These tests still cover the ``_default_gather_inputs`` helper in
``argosy/orchestrator/loops/daily_brief.py``. That helper is the
production writer for the ``investor_events`` table (SEC Form 4 /
TipRanks / 13F / CapitolTrades / Finnhub news) — the home-page signal
bullet (``argosy/api/routes/advisor.py``) reads from that table. The
helper is preserved as a standalone collector for a future cadence
loop or direct integration into the runner.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

# ``configure_logging()`` bridges structlog to the stdlib ``logging``
# system that pytest's ``caplog`` fixture inspects. Without this
# bootstrap, the per-ticker outage WARNINGs only land on stderr and
# the ``caplog.records`` assertions below see an empty string. Pre-W9
# this happened implicitly via ``from argosy.api import events`` at
# module scope (needed by the now-deleted orchestration test). With
# the orchestration test gone, the bootstrap is preserved explicitly
# as a fixture-level precondition for the investor-events tests.
from argosy.logging import configure_logging
from argosy.state import db as db_mod

configure_logging()


# ----------------------------------------------------------------------
# _default_gather_inputs — investor-event adapter graceful degradation.
# Covers the Phase 4 review fix where MissingDataSourceError was used
# inside the inner per-ticker try/except but never imported at module
# scope. Without the import, an outage on the *first* ticker turns into
# a NameError swallowed by the outer ``except Exception``, dropping the
# entire section instead of degrading per-ticker.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_gather_inputs_form4_outage_degrades(
    monkeypatch: pytest.MonkeyPatch, engine: None, caplog: pytest.LogCaptureFixture
) -> None:
    from argosy.adapters import MissingDataSourceError
    from argosy.adapters.data import (
        sec_13f_adapter,
        sec_form4_adapter,
        tipranks_adapter,
    )
    from argosy.orchestrator.loops import daily_brief as db_loop
    from argosy.state.models import User, UserContext

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        # Seed a 13F watchlist so the third (Sec 13F) adapter branch is
        # exercised by the regression test, alongside Form 4 and TipRanks.
        session.add(
            UserContext(
                user_id="ariel",
                identity_yaml="thirteen_f_watchlist:\n  - '0001067983'\n",
            )
        )
        await session.commit()

    # Pretend a TSV exists with two tickers; we don't actually parse a
    # file — we monkeypatch the loader to return a synthetic snapshot.
    class _FakePos:
        def __init__(self, t: str) -> None:
            self.ticker = t
            self.quantity = 1
            self.market_value = 0
            self.value = 0
            self.account = ""

    class _FakeSnap:
        positions = [_FakePos("NVDA"), _FakePos("AAPL")]

    monkeypatch.setattr(db_loop, "_find_latest_tsv", lambda: "fake.tsv")
    import argosy.ingest.tsv as ingest_tsv
    monkeypatch.setattr(ingest_tsv, "parse_portfolio_tsv", lambda _p: _FakeSnap())

    # Failing Form 4 adapter — first ticker outage must NOT abort the
    # loop; subsequent tickers must continue to be attempted.
    class _OutageForm4:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_recent_form4_for_ticker(
            self, ticker: str, *, days: int = 30
        ) -> list[dict]:
            self.calls.append(ticker)
            raise MissingDataSourceError(f"simulated SEC outage for {ticker}")

    outage_form4 = _OutageForm4()
    monkeypatch.setattr(
        sec_form4_adapter, "SecForm4Adapter", lambda: outage_form4
    )

    # Failing TipRanks adapter — same pattern.
    class _OutageTipRanks:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_analyst_consensus(self, ticker: str) -> dict:
            self.calls.append(ticker)
            raise MissingDataSourceError(f"simulated TipRanks outage for {ticker}")

    outage_tr = _OutageTipRanks()
    monkeypatch.setattr(tipranks_adapter, "TipRanksAdapter", lambda: outage_tr)

    # Failing 13F adapter — verify the per-CIK branch handles outage too.
    class _OutageSec13F:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_filer_history(
            self, cik: str, *, quarters: int = 1
        ) -> list[dict]:
            self.calls.append(cik)
            raise MissingDataSourceError(f"simulated SEC 13F outage for {cik}")

    outage_13f = _OutageSec13F()
    monkeypatch.setattr(sec_13f_adapter, "Sec13FAdapter", lambda: outage_13f)

    import logging

    with caplog.at_level(logging.WARNING, logger="argosy.loops.daily_brief"):
        inputs = await db_loop._default_gather_inputs("ariel")

    # Both tickers should have been attempted, despite the first one
    # raising. The empty per-ticker dict is the graceful-degradation
    # outcome — neither a crash nor silent abort after the first miss.
    assert outage_form4.calls == ["AAPL", "NVDA"]
    assert outage_tr.calls == ["AAPL", "NVDA"]
    # The 13F filer must have been hit too, despite Form 4 / TipRanks
    # both raising before it.
    assert outage_13f.calls == ["0001067983"]
    assert inputs.insider_activity == {}
    assert inputs.analyst_signals == {}
    assert inputs.thirteen_f_watchlist == []
    # Other fields should still be populated from the fake snapshot.
    assert sorted(inputs.tickers) == ["AAPL", "NVDA"]
    # Per-ticker outages must be logged at WARNING with the
    # ``form4_skipped`` / ``tipranks_skipped`` / ``sec13f_skipped`` event
    # names — NOT the catch-all ``form4_failed`` / ``tipranks_failed`` /
    # ``sec13f_failed`` (those would indicate the inner try-except didn't
    # catch the MissingDataSourceError, which is the regression we
    # introduced this fixture to guard against).
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "form4_skipped" in log_text, log_text
    assert "form4_failed" not in log_text, log_text
    assert "tipranks_skipped" in log_text, log_text
    assert "tipranks_failed" not in log_text, log_text
    assert "sec13f_skipped" in log_text, log_text
    assert "sec13f_failed" not in log_text, log_text


# ----------------------------------------------------------------------
# Investor-event persistence (Phase 4 / home-brief signal-bullet path).
# Verifies _default_gather_inputs writes through to the investor_events
# table after a successful adapter pull, so the home brief can query
# durable state instead of depending on the adapter cache TTL.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_gather_inputs_capitoltrades_outage_degrades(
    monkeypatch: pytest.MonkeyPatch, engine: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A CapitolTrades adapter outage on the first ticker MUST NOT
    abort the loop; subsequent tickers must continue to be attempted,
    and the per-ticker outage must be logged at WARNING via
    ``daily_brief.capitoltrades_skipped`` (NOT the catch-all
    ``capitoltrades_failed``)."""
    from argosy.adapters import MissingDataSourceError
    from argosy.adapters.data import capitoltrades_adapter
    from argosy.orchestrator.loops import daily_brief as db_loop
    from argosy.state.models import User

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    class _FakePos:
        def __init__(self, t: str) -> None:
            self.ticker = t
            self.quantity = 1
            self.market_value = 0
            self.value = 0
            self.account = ""

    class _FakeSnap:
        positions = [_FakePos("NVDA"), _FakePos("AAPL")]

    monkeypatch.setattr(db_loop, "_find_latest_tsv", lambda: "fake.tsv")
    import argosy.ingest.tsv as ingest_tsv
    monkeypatch.setattr(ingest_tsv, "parse_portfolio_tsv", lambda _p: _FakeSnap())

    class _OutageCT:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def list_trades_for_ticker(
            self, ticker: str, *, days: int = 30
        ) -> list[dict]:
            self.calls.append(ticker)
            raise MissingDataSourceError(f"simulated CapitolTrades outage for {ticker}")

    outage_ct = _OutageCT()
    monkeypatch.setattr(
        capitoltrades_adapter, "CapitolTradesAdapter", lambda: outage_ct
    )

    import logging

    with caplog.at_level(logging.WARNING, logger="argosy.loops.daily_brief"):
        await db_loop._default_gather_inputs("ariel")

    assert outage_ct.calls == ["AAPL", "NVDA"]
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "capitoltrades_skipped" in log_text, log_text
    assert "capitoltrades_failed" not in log_text, log_text


@pytest.mark.asyncio
async def test_default_gather_inputs_persists_capitoltrades_events(
    monkeypatch: pytest.MonkeyPatch, engine: None
) -> None:
    """Successful CapitolTrades pull must write through to investor_events
    so the home brief signal bullet can surface the most-recent
    politician trade."""
    from argosy.adapters.data import capitoltrades_adapter
    from argosy.orchestrator.loops import daily_brief as db_loop
    from argosy.state.models import InvestorEvent, User

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    class _FakePos:
        def __init__(self, t: str) -> None:
            self.ticker = t
            self.quantity = 1
            self.market_value = 0
            self.value = 0
            self.account = ""

    class _FakeSnap:
        positions = [_FakePos("NVDA")]

    monkeypatch.setattr(db_loop, "_find_latest_tsv", lambda: "fake.tsv")
    import argosy.ingest.tsv as ingest_tsv
    monkeypatch.setattr(ingest_tsv, "parse_portfolio_tsv", lambda _p: _FakeSnap())

    class _StubCT:
        async def list_trades_for_ticker(
            self, ticker: str, *, days: int = 30
        ) -> list[dict]:
            return [
                {
                    "politician_name": "Nancy Pelosi",
                    "ticker": ticker,
                    "transaction_type": "buy",
                    "transaction_date": "2026-04-30",
                    "amount_range": "$1M-$5M",
                }
            ]

    monkeypatch.setattr(
        capitoltrades_adapter, "CapitolTradesAdapter", lambda: _StubCT()
    )

    await db_loop._default_gather_inputs("ariel")

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(InvestorEvent).where(
                    InvestorEvent.user_id == "ariel",
                    InvestorEvent.source == "capitoltrades",
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert "Nancy Pelosi" in rows[0].headline
    assert rows[0].ticker == "NVDA"


@pytest.mark.asyncio
async def test_default_gather_inputs_persists_news_events(
    monkeypatch: pytest.MonkeyPatch, engine: None
) -> None:
    """Successful Finnhub news pull must write through to
    investor_events under source=``news`` so the home brief signal
    bullet can surface the most-recent headline alongside Form 4 / 13F
    / TipRanks / CapitolTrades."""
    from argosy.adapters.data import finnhub_adapter
    from argosy.orchestrator.loops import daily_brief as db_loop
    from argosy.state.models import InvestorEvent, User

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    class _FakePos:
        def __init__(self, t: str) -> None:
            self.ticker = t
            self.quantity = 1
            self.market_value = 0
            self.value = 0
            self.account = ""

    class _FakeSnap:
        positions = [_FakePos("NVDA")]

    monkeypatch.setattr(db_loop, "_find_latest_tsv", lambda: "fake.tsv")
    import argosy.ingest.tsv as ingest_tsv
    monkeypatch.setattr(ingest_tsv, "parse_portfolio_tsv", lambda _p: _FakeSnap())

    class _StubFinnhub:
        async def get_company_news(self, symbol, *, start, end, ttl_seconds=900):
            return [
                {
                    "headline": "NVDA earnings beat the street",
                    "summary": "Q1 EPS beat by 5%.",
                    "url": "https://www.reuters.com/x/1",
                    "source": "Reuters",
                    "datetime": 1746360000,  # 2025-05-04 ish UTC
                }
            ]

    monkeypatch.setattr(finnhub_adapter, "FinnhubAdapter", lambda: _StubFinnhub())

    await db_loop._default_gather_inputs("ariel")

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(InvestorEvent).where(
                    InvestorEvent.user_id == "ariel",
                    InvestorEvent.source == "news",
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].ticker == "NVDA"
    assert "earnings beat" in rows[0].headline
    assert "Reuters" in rows[0].headline


@pytest.mark.asyncio
async def test_default_gather_inputs_persists_investor_events(
    monkeypatch: pytest.MonkeyPatch, engine: None
) -> None:
    from argosy.adapters.data import sec_form4_adapter, tipranks_adapter
    from argosy.orchestrator.loops import daily_brief as db_loop
    from argosy.state.models import InvestorEvent, User

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    class _FakePos:
        def __init__(self, t: str) -> None:
            self.ticker = t
            self.quantity = 1
            self.market_value = 0
            self.value = 0
            self.account = ""

    class _FakeSnap:
        positions = [_FakePos("NVDA")]

    monkeypatch.setattr(db_loop, "_find_latest_tsv", lambda: "fake.tsv")
    import argosy.ingest.tsv as ingest_tsv
    monkeypatch.setattr(ingest_tsv, "parse_portfolio_tsv", lambda _p: _FakeSnap())

    class _StubForm4:
        async def get_recent_form4_for_ticker(
            self, ticker: str, *, days: int = 30
        ) -> list[dict]:
            return [
                {
                    "filer_name": "Jensen Huang",
                    "role": "officer (CEO)",
                    "ticker": ticker,
                    "transaction_date": "2026-04-30",
                    "transaction_code": "P",
                    "transaction_kind": "purchase",
                    "shares": 10000,
                    "price_per_share": 912.34,
                    "value_usd": 9123400.0,
                    "post_transaction_holdings": 100000,
                }
            ]

    class _StubTipRanks:
        async def get_analyst_consensus(self, ticker: str) -> dict:
            return {
                "ticker": ticker,
                "consensus_label": "Strong Buy",
                "average_price_target": 950.0,
                "num_buy": 30,
                "num_hold": 5,
                "num_sell": 1,
                "last_updated": "2026-05-01",
            }

    monkeypatch.setattr(sec_form4_adapter, "SecForm4Adapter", lambda: _StubForm4())
    monkeypatch.setattr(tipranks_adapter, "TipRanksAdapter", lambda: _StubTipRanks())

    await db_loop._default_gather_inputs("ariel")

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(InvestorEvent).where(InvestorEvent.user_id == "ariel")
            )
        ).scalars().all()
    sources = {r.source for r in rows}
    assert "sec_form4" in sources
    assert "tipranks" in sources
    # Form 4 row should carry the headline our mapper produces.
    f4 = next(r for r in rows if r.source == "sec_form4")
    assert "Jensen Huang" in f4.headline
    # The ``transaction_kind=purchase`` mapping must surface as the verb
    # ``bought`` (not the raw transaction code ``P``) so the bullet
    # reads as a sentence.
    assert "bought" in f4.headline, f4.headline
    # Shares are formatted with thousands separators.
    assert "10,000" in f4.headline, f4.headline
    # Price clause renders with two decimals.
    assert "$912.34" in f4.headline, f4.headline
    assert f4.ticker == "NVDA"
    # payload_json round-trips: structured fields survive serialization.
    import json as _json
    parsed = _json.loads(f4.payload_json)
    assert parsed["filer_name"] == "Jensen Huang"
    assert parsed["transaction_code"] == "P"
    # TipRanks row carries consensus label + ticker.
    tr = next(r for r in rows if r.source == "tipranks")
    assert "Strong Buy" in tr.headline
    assert tr.ticker == "NVDA"


@pytest.mark.asyncio
async def test_default_gather_inputs_dedups_repeat_pulls(
    monkeypatch: pytest.MonkeyPatch, engine: None
) -> None:
    """Same Form 4 row landing on two consecutive daily-brief ticks must
    produce one investor_events row, not two — the ``unique_key`` +
    ON CONFLICT DO NOTHING gating keeps the table from growing
    unboundedly across repeat pulls."""
    from argosy.adapters.data import sec_form4_adapter
    from argosy.orchestrator.loops import daily_brief as db_loop
    from argosy.state.models import InvestorEvent, User

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    class _FakePos:
        def __init__(self, t: str) -> None:
            self.ticker = t
            self.quantity = 1
            self.market_value = 0
            self.value = 0
            self.account = ""

    class _FakeSnap:
        positions = [_FakePos("NVDA")]

    monkeypatch.setattr(db_loop, "_find_latest_tsv", lambda: "fake.tsv")
    import argosy.ingest.tsv as ingest_tsv
    monkeypatch.setattr(ingest_tsv, "parse_portfolio_tsv", lambda _p: _FakeSnap())

    class _StubForm4:
        async def get_recent_form4_for_ticker(
            self, ticker: str, *, days: int = 30
        ) -> list[dict]:
            # Same row both times — this is exactly the production
            # behavior: a 30-day lookback returns the same insider trade
            # on every tick within the window.
            return [
                {
                    "filer_name": "Jensen Huang",
                    "ticker": ticker,
                    "transaction_date": "2026-04-30",
                    "transaction_code": "P",
                    "transaction_kind": "purchase",
                    "shares": 10000,
                    "price_per_share": 912.34,
                    "accession_number": "0001045810-26-000123",
                }
            ]

    monkeypatch.setattr(sec_form4_adapter, "SecForm4Adapter", lambda: _StubForm4())

    # Two consecutive ticks with the SAME stub data.
    await db_loop._default_gather_inputs("ariel")
    await db_loop._default_gather_inputs("ariel")

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(InvestorEvent).where(
                    InvestorEvent.user_id == "ariel",
                    InvestorEvent.source == "sec_form4",
                )
            )
        ).scalars().all()
    # Despite two ticks, only one row — dedup by (user_id, source, unique_key).
    assert len(rows) == 1, [r.headline for r in rows]
