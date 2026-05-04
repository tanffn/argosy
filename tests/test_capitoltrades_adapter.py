"""capitoltrades.com (US politicians' STOCK Act trades) adapter tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.capitoltrades_adapter import (
    CapitolTradesAdapter,
    _coerce_iso_date,
    _normalize_tx_type,
    _on_or_after,
    _parse_trades_html,
)


# ----------------------------------------------------------------------
# Fixture HTML
# ----------------------------------------------------------------------


def _trade_row_html(
    *,
    name: str,
    party: str,
    state: str,
    ticker: str,
    issuer: str,
    publish: str,
    traded: str,
    tx_type: str,
    amount_range: str,
) -> str:
    return f"""
    <tr>
      <td>
        <a>{name}</a>
        <span class="q-field-party">{party}</span>
        <span>House <span>{state}</span></span>
      </td>
      <td>
        <a>{issuer}</a>
        <span class="q-field-issuer-ticker">{ticker}:US</span>
      </td>
      <td>{publish}</td>
      <td>{traded}</td>
      <td>1 day</td>
      <td>self</td>
      <td>{tx_type}</td>
      <td>{amount_range}</td>
      <td>$185.00</td>
    </tr>
    """


def _build_fixture_html(rows_html: str) -> str:
    return f"""<!doctype html><html><body>
      <table class="q-table">
        <thead><tr>
          <th>Politician</th><th>Traded Issuer</th><th>Published</th>
          <th>Traded</th><th>Filed after</th><th>Owner</th>
          <th>Type</th><th>Size</th><th>Price</th>
        </tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </body></html>"""


_TODAY_ISO = datetime.now(timezone.utc).date().isoformat()
_RECENT_ISO = (datetime.now(timezone.utc).date() - timedelta(days=5)).isoformat()
_OLD_ISO = (datetime.now(timezone.utc).date() - timedelta(days=120)).isoformat()


_FIXTURE_HTML = _build_fixture_html(
    _trade_row_html(
        name="Nancy Pelosi", party="Democrat", state="CA",
        ticker="NVDA", issuer="NVIDIA Corp",
        publish=_TODAY_ISO, traded=_RECENT_ISO,
        tx_type="buy", amount_range="$1M – $5M",
    )
    + _trade_row_html(
        name="Ron Wyden", party="Democrat", state="OR",
        ticker="AAPL", issuer="Apple Inc.",
        publish=_TODAY_ISO, traded=_RECENT_ISO,
        tx_type="sell", amount_range="$15K – $50K",
    )
    + _trade_row_html(
        name="Joe Generic", party="Republican", state="TX",
        ticker="TSLA", issuer="Tesla Inc.",
        publish=_OLD_ISO, traded=_OLD_ISO,
        tx_type="buy", amount_range="$1K – $15K",
    )
)


# ----------------------------------------------------------------------
# Fake HTTP shim
# ----------------------------------------------------------------------


class _FakeResp:
    def __init__(self, *, status: int = 200, text: str = "") -> None:
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")


class _FakeHttp:
    def __init__(self, text: str, *, status: int = 200) -> None:
        self._text = text
        self._status = status
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResp:
        self.calls.append((url, params or {}))
        return _FakeResp(status=self._status, text=self._text)


class _FailingHttp:
    async def get(self, *args: Any, **kwargs: Any) -> _FakeResp:
        raise OSError("DNS failure (simulated)")


# ----------------------------------------------------------------------
# Pure-parsing tests
# ----------------------------------------------------------------------


def test_parse_trades_html_extracts_rows() -> None:
    rows = _parse_trades_html(_FIXTURE_HTML, source_url="https://x")
    assert len(rows) == 3
    nvda = next(r for r in rows if r["ticker"] == "NVDA")
    assert nvda["politician_name"] == "Nancy Pelosi"
    assert nvda["party"] == "Democrat"
    assert nvda["state"] == "CA"
    assert nvda["transaction_type"] == "buy"
    assert nvda["amount_range"] == "$1M – $5M"
    assert nvda["transaction_date"] == _RECENT_ISO


def test_parse_trades_html_no_table_raises() -> None:
    with pytest.raises(MissingDataSourceError):
        _parse_trades_html("<html><body>nothing</body></html>",
                           source_url="https://x")


def test_coerce_iso_date_handles_formats() -> None:
    assert _coerce_iso_date("2026-04-30") == "2026-04-30"
    assert _coerce_iso_date("30 Apr 2026") == "2026-04-30"
    assert _coerce_iso_date("Apr 30 2026") == "2026-04-30"
    assert _coerce_iso_date("") == ""
    assert _coerce_iso_date("garbage") == ""


def test_coerce_iso_date_relative() -> None:
    today = datetime.now(timezone.utc).date()
    assert _coerce_iso_date("Today") == today.isoformat()
    assert _coerce_iso_date("Yesterday") == (today - timedelta(days=1)).isoformat()


def test_normalize_tx_type() -> None:
    assert _normalize_tx_type("Buy") == "buy"
    assert _normalize_tx_type("sale") == "sell"
    assert _normalize_tx_type("partial purchase") == "buy"


def test_on_or_after() -> None:
    cutoff = date(2026, 4, 1)
    assert _on_or_after("2026-04-30", cutoff) is True
    assert _on_or_after("2026-03-01", cutoff) is False
    assert _on_or_after("", cutoff) is False


# ----------------------------------------------------------------------
# Adapter tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_recent_trades_filters_by_window(engine: None) -> None:
    fake = _FakeHttp(_FIXTURE_HTML)
    adapter = CapitolTradesAdapter(http_client=fake)
    rows = await adapter.list_recent_trades(days=30)
    # The 120-day-old trade is filtered out.
    assert len(rows) == 2
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"NVDA", "AAPL"}


@pytest.mark.asyncio
async def test_list_recent_trades_caches(engine: None) -> None:
    fake = _FakeHttp(_FIXTURE_HTML)
    adapter = CapitolTradesAdapter(http_client=fake)
    rows1 = await adapter.list_recent_trades(days=30)
    rows2 = await adapter.list_recent_trades(days=30)
    assert rows1 == rows2
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_list_trades_for_ticker(engine: None) -> None:
    fake = _FakeHttp(_FIXTURE_HTML)
    adapter = CapitolTradesAdapter(http_client=fake)
    rows = await adapter.list_trades_for_ticker("NVDA", days=30)
    assert len(rows) == 1
    assert rows[0]["politician_name"] == "Nancy Pelosi"


@pytest.mark.asyncio
async def test_list_trades_for_politician(engine: None) -> None:
    fake = _FakeHttp(_FIXTURE_HTML)
    adapter = CapitolTradesAdapter(http_client=fake)
    rows = await adapter.list_trades_for_politician("nancy-pelosi")
    # Politician page returns the same fixture; we just verify the URL flow.
    assert any(r["politician_name"] == "Nancy Pelosi" for r in rows)


@pytest.mark.asyncio
async def test_outage_raises(engine: None) -> None:
    adapter = CapitolTradesAdapter(http_client=_FailingHttp())
    with pytest.raises(MissingDataSourceError):
        await adapter.list_recent_trades()


@pytest.mark.asyncio
async def test_bad_status_raises(engine: None) -> None:
    adapter = CapitolTradesAdapter(http_client=_FakeHttp("oops", status=503))
    with pytest.raises(MissingDataSourceError):
        await adapter.list_recent_trades()


@pytest.mark.asyncio
async def test_invalid_inputs(engine: None) -> None:
    adapter = CapitolTradesAdapter(http_client=_FailingHttp())
    with pytest.raises(ValueError):
        await adapter.list_recent_trades(days=0)
    with pytest.raises(ValueError):
        await adapter.list_trades_for_politician("")
    with pytest.raises(ValueError):
        await adapter.list_trades_for_ticker("")
    with pytest.raises(ValueError):
        await adapter.list_trades_for_ticker("NVDA", days=0)
