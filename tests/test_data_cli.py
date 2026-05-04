"""`argosy data ...` CLI tests using Typer's CliRunner.

Patches the per-command adapter factories so no network access happens.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from argosy.adapters import MissingDataSourceError
from argosy.cli import data as data_cli


# ----------------------------------------------------------------------
# Fakes for each adapter
# ----------------------------------------------------------------------


class _FakeSec13F:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[str] = []

    async def list_recent_13f(self, *, days: int = 90, ttl_seconds: int = 0) -> list[dict[str, Any]]:
        self.calls.append(f"recent:days={days}")
        if self._fail:
            raise MissingDataSourceError("simulated outage")
        return [
            {
                "cik": "1067983",
                "fund_name": "BERKSHIRE HATHAWAY INC",
                "period_of_report": "2025-12-31",
                "accession_number": "0001067983-25-000002",
                "filed_at": "2026-02-14",
                "document_url": "https://x",
            }
        ]

    async def get_filer_history(
        self, cik: str, *, quarters: int = 4, ttl_seconds: int = 0
    ) -> list[dict[str, Any]]:
        self.calls.append(f"filer:{cik}:q={quarters}")
        if self._fail:
            raise MissingDataSourceError("simulated outage")
        return [
            {
                "cik": cik,
                "fund_name": "BERKSHIRE HATHAWAY INC",
                "period_of_report": f"2025-Q{q+1}",
                "accession_number": f"00010679-25-{q:06d}",
                "filed_at": f"2026-{q+1:02d}-14",
                "document_url": "https://x",
            }
            for q in range(quarters)
        ]


class _FakeForm4:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[str] = []

    async def get_recent_form4_for_ticker(
        self, ticker: str, *, days: int = 30, ttl_seconds: int = 0
    ) -> list[dict[str, Any]]:
        self.calls.append(f"ticker:{ticker}:days={days}")
        if self._fail:
            raise MissingDataSourceError("simulated outage")
        return [
            {
                "filer_name": "HUANG JEN HSUN",
                "role": "director, officer",
                "ticker": ticker.upper(),
                "transaction_date": "2026-04-15",
                "transaction_code": "S",
                "transaction_kind": "sale",
                "shares": 120000,
                "price_per_share": 950.5,
                "value_usd": 120000 * 950.5,
                "post_transaction_holdings": 800000,
            }
        ]

    async def get_recent_form4_for_filer(
        self, cik: str, *, days: int = 90, ttl_seconds: int = 0
    ) -> list[dict[str, Any]]:
        self.calls.append(f"filer:{cik}:days={days}")
        if self._fail:
            raise MissingDataSourceError("simulated outage")
        return [
            {
                "filer_name": "X",
                "role": "officer",
                "ticker": "ACME",
                "transaction_date": "2026-04-15",
                "transaction_code": "P",
                "shares": 100,
                "price_per_share": 10.0,
                "value_usd": 1000.0,
                "post_transaction_holdings": 0,
            }
        ]


class _FakeCapitolTrades:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[str] = []

    async def list_recent_trades(self, *, days: int = 30, ttl_seconds: int = 0) -> list[dict[str, Any]]:
        self.calls.append(f"recent:days={days}")
        if self._fail:
            raise MissingDataSourceError("simulated outage")
        return [
            {
                "politician_name": "Nancy Pelosi",
                "party": "Democrat",
                "state": "CA",
                "ticker": "NVDA",
                "transaction_type": "buy",
                "transaction_date": "2026-04-30",
                "disclosure_date": "2026-05-01",
                "amount_range": "$1M – $5M",
            }
        ]

    async def list_trades_for_ticker(
        self, ticker: str, *, days: int = 365, ttl_seconds: int = 0
    ) -> list[dict[str, Any]]:
        self.calls.append(f"ticker:{ticker}:days={days}")
        if self._fail:
            raise MissingDataSourceError("simulated outage")
        return [
            {
                "politician_name": "X",
                "party": "Democrat",
                "state": "CA",
                "ticker": ticker.upper(),
                "transaction_type": "buy",
                "transaction_date": "2026-04-30",
                "disclosure_date": "2026-05-01",
                "amount_range": "$1K – $15K",
            }
        ]

    async def list_trades_for_politician(
        self, slug: str, *, ttl_seconds: int = 0
    ) -> list[dict[str, Any]]:
        self.calls.append(f"slug:{slug}")
        if self._fail:
            raise MissingDataSourceError("simulated outage")
        return [
            {
                "politician_name": slug,
                "party": "?",
                "state": "?",
                "ticker": "AAPL",
                "transaction_type": "sell",
                "transaction_date": "2026-04-30",
                "disclosure_date": "2026-05-01",
                "amount_range": "$15K – $50K",
            }
        ]


class _FakeTipRanks:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    async def get_analyst_consensus(
        self, ticker: str, *, ttl_seconds: int = 0
    ) -> dict[str, Any]:
        if self._fail:
            raise MissingDataSourceError("simulated outage")
        return {
            "ticker": ticker.upper(),
            "consensus_label": "Strong Buy",
            "average_price_target": 1100.0,
            "num_buy": 30,
            "num_hold": 5,
            "num_sell": 1,
            "last_updated": "2026-04-30",
            "source_url": "https://x",
        }

    async def get_blogger_sentiment(
        self, ticker: str, *, ttl_seconds: int = 0
    ) -> dict[str, Any]:
        if self._fail:
            raise MissingDataSourceError("simulated outage")
        return {"ticker": ticker.upper(), "bullish_pct": 78.0, "bearish_pct": 22.0}

    async def get_hedge_fund_signal(
        self, ticker: str, *, ttl_seconds: int = 0
    ) -> dict[str, Any]:
        if self._fail:
            raise MissingDataSourceError("simulated outage")
        return {
            "ticker": ticker.upper(),
            "hedge_funds_holding": 84,
            "recent_change": "increased",
        }


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_sec13f(monkeypatch: pytest.MonkeyPatch) -> _FakeSec13F:
    fake = _FakeSec13F()
    monkeypatch.setattr(data_cli, "_sec13f", lambda: fake)
    return fake


@pytest.fixture
def fake_form4(monkeypatch: pytest.MonkeyPatch) -> _FakeForm4:
    fake = _FakeForm4()
    monkeypatch.setattr(data_cli, "_form4", lambda: fake)
    return fake


@pytest.fixture
def fake_capitoltrades(monkeypatch: pytest.MonkeyPatch) -> _FakeCapitolTrades:
    fake = _FakeCapitolTrades()
    monkeypatch.setattr(data_cli, "_capitoltrades", lambda: fake)
    return fake


@pytest.fixture
def fake_tipranks(monkeypatch: pytest.MonkeyPatch) -> _FakeTipRanks:
    fake = _FakeTipRanks()
    monkeypatch.setattr(data_cli, "_tipranks", lambda: fake)
    return fake


# ----------------------------------------------------------------------
# 13F commands
# ----------------------------------------------------------------------


def test_data_13f_recent_prints_rows(
    cli_runner: CliRunner, fake_sec13f: _FakeSec13F
) -> None:
    result = cli_runner.invoke(data_cli.app, ["13f", "recent"])
    assert result.exit_code == 0, result.output
    assert "BERKSHIRE" in result.output
    assert "0001067983-25-000002" in result.output


def test_data_13f_recent_outage(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(data_cli, "_sec13f", lambda: _FakeSec13F(fail=True))
    result = cli_runner.invoke(data_cli.app, ["13f", "recent"])
    assert result.exit_code == 2
    assert "sec_13f error" in result.output or "simulated outage" in result.output


def test_data_13f_filer(
    cli_runner: CliRunner, fake_sec13f: _FakeSec13F
) -> None:
    result = cli_runner.invoke(
        data_cli.app, ["13f", "filer", "0001067983", "--quarters", "2"]
    )
    assert result.exit_code == 0
    assert "BERKSHIRE" in result.output


# ----------------------------------------------------------------------
# Form 4 commands
# ----------------------------------------------------------------------


def test_data_form4_ticker(
    cli_runner: CliRunner, fake_form4: _FakeForm4
) -> None:
    result = cli_runner.invoke(data_cli.app, ["form4", "ticker", "NVDA"])
    assert result.exit_code == 0, result.output
    assert "HUANG" in result.output
    assert "120000" in result.output


def test_data_form4_filer(
    cli_runner: CliRunner, fake_form4: _FakeForm4
) -> None:
    result = cli_runner.invoke(data_cli.app, ["form4", "filer", "0001045810"])
    assert result.exit_code == 0
    assert "ACME" in result.output


def test_data_form4_outage(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(data_cli, "_form4", lambda: _FakeForm4(fail=True))
    result = cli_runner.invoke(data_cli.app, ["form4", "ticker", "NVDA"])
    assert result.exit_code == 2


# ----------------------------------------------------------------------
# Politicians command
# ----------------------------------------------------------------------


def test_data_politicians_default(
    cli_runner: CliRunner, fake_capitoltrades: _FakeCapitolTrades
) -> None:
    result = cli_runner.invoke(data_cli.app, ["politicians"])
    assert result.exit_code == 0
    assert "Nancy Pelosi" in result.output


def test_data_politicians_by_ticker(
    cli_runner: CliRunner, fake_capitoltrades: _FakeCapitolTrades
) -> None:
    result = cli_runner.invoke(data_cli.app, ["politicians", "--ticker", "NVDA"])
    assert result.exit_code == 0
    assert "NVDA" in result.output


def test_data_politicians_by_slug(
    cli_runner: CliRunner, fake_capitoltrades: _FakeCapitolTrades
) -> None:
    result = cli_runner.invoke(
        data_cli.app, ["politicians", "--politician", "nancy-pelosi"]
    )
    assert result.exit_code == 0
    assert "nancy-pelosi" in result.output


def test_data_politicians_outage(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        data_cli, "_capitoltrades", lambda: _FakeCapitolTrades(fail=True)
    )
    result = cli_runner.invoke(data_cli.app, ["politicians"])
    assert result.exit_code == 2


# ----------------------------------------------------------------------
# Analyst command
# ----------------------------------------------------------------------


def test_data_analyst(
    cli_runner: CliRunner, fake_tipranks: _FakeTipRanks
) -> None:
    result = cli_runner.invoke(data_cli.app, ["analyst", "NVDA"])
    assert result.exit_code == 0, result.output
    assert "Strong Buy" in result.output
    assert "78.0" in result.output  # blogger bullish_pct
    assert "84" in result.output  # hedge funds holding


def test_data_analyst_json(
    cli_runner: CliRunner, fake_tipranks: _FakeTipRanks
) -> None:
    result = cli_runner.invoke(data_cli.app, ["analyst", "NVDA", "--json"])
    assert result.exit_code == 0
    assert '"consensus_label": "Strong Buy"' in result.output


def test_data_analyst_outage(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(data_cli, "_tipranks", lambda: _FakeTipRanks(fail=True))
    result = cli_runner.invoke(data_cli.app, ["analyst", "NVDA"])
    assert result.exit_code == 2
