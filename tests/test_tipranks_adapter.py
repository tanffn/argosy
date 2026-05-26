"""TipRanks (analyst sentiment aggregator) adapter tests.

We never call live tipranks.com; tests inject a fake http client and
exercise the parser against synthetic ``__NEXT_DATA__`` blobs and
fallback-text payloads.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.tipranks_adapter import (
    TipRanksAdapter,
    _extract_next_data,
    _normalize_change,
    _normalize_consensus,
    _parse_analyst_consensus,
    _parse_blogger_sentiment,
    _parse_hedge_fund_signal,
)


# ----------------------------------------------------------------------
# Synthetic __NEXT_DATA__ payloads
# ----------------------------------------------------------------------


def _next_data_html(payload: dict[str, Any]) -> str:
    """Wrap a JSON payload in TipRanks's typical __NEXT_DATA__ envelope."""
    j = json.dumps(payload)
    return f"""<!doctype html><html><body>
    <main>...</main>
    <script id="__NEXT_DATA__" type="application/json">{j}</script>
    </body></html>"""


_CONSENSUS_PAYLOAD = {
    "props": {
        "pageProps": {
            "data": {
                "consensuses": [
                    {
                        "rating": "Strong Buy",
                        "nB": 30,
                        "nH": 5,
                        "nS": 1,
                        "d": "2026-04-30",
                    }
                ],
                "priceTargets": [
                    {"priceTarget": 1100.50}
                ],
            }
        }
    }
}


_BLOGGER_PAYLOAD = {
    "props": {
        "pageProps": {
            "data": {
                "bloggerSentiment": {
                    "bullishPct": 78.0,
                    "bearishPct": 22.0,
                }
            }
        }
    }
}


_HEDGE_PAYLOAD = {
    "props": {
        "pageProps": {
            "data": {
                "hedgeFundSignal": {
                    "hedgeFundsHolding": 84,
                    "recentChange": "increased",
                }
            }
        }
    }
}


# Fallback HTML — no __NEXT_DATA__; relies on regex extraction.
_FALLBACK_CONSENSUS_HTML = """<!doctype html><html><body>
<main>
  <p>Analyst Consensus: Strong Buy</p>
  <p>Average Price Target: $1100.50</p>
  <p>Based on 30 Buy, 5 Hold, 1 Sell ratings.</p>
</main>
</body></html>"""


_FALLBACK_BLOGGER_HTML = """<!doctype html><html><body>
<main>
  <p>Bloggers are 78% bullish and 22% bearish on this stock.</p>
</main>
</body></html>"""


_FALLBACK_HEDGE_HTML = """<!doctype html><html><body>
<main>
  <p>84 hedge funds hold this stock; the position increased last quarter.</p>
</main>
</body></html>"""


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
        self.calls: list[str] = []

    async def get(self, url: str, **_kwargs: Any) -> _FakeResp:
        self.calls.append(url)
        return _FakeResp(status=self._status, text=self._text)


class _FailingHttp:
    async def get(self, *args: Any, **kwargs: Any) -> _FakeResp:
        raise OSError("DNS failure (simulated)")


# ----------------------------------------------------------------------
# Pure-parsing tests
# ----------------------------------------------------------------------


def test_normalize_consensus() -> None:
    assert _normalize_consensus("Strong Buy") == "Strong Buy"
    assert _normalize_consensus("BUY") == "Moderate Buy"
    assert _normalize_consensus("hold") == "Hold"
    assert _normalize_consensus("Strong Sell") == "Strong Sell"


def test_normalize_change() -> None:
    assert _normalize_change("increased") == "increased"
    assert _normalize_change("Decreased") == "decreased"
    assert _normalize_change("unchanged") == "unchanged"
    assert _normalize_change("???") == "unknown"


def test_extract_next_data_present() -> None:
    html = _next_data_html({"hello": "world"})
    out = _extract_next_data(html)
    assert out == {"hello": "world"}


def test_extract_next_data_absent() -> None:
    assert _extract_next_data("<html></html>") is None


def test_parse_analyst_consensus_from_next_data() -> None:
    out = _parse_analyst_consensus(_next_data_html(_CONSENSUS_PAYLOAD))
    assert out["consensus_label"] == "Strong Buy"
    assert out["average_price_target"] == pytest.approx(1100.50)
    assert out["num_buy"] == 30
    assert out["num_hold"] == 5
    assert out["num_sell"] == 1
    assert out["last_updated"] == "2026-04-30"


def test_parse_analyst_consensus_from_fallback_html() -> None:
    out = _parse_analyst_consensus(_FALLBACK_CONSENSUS_HTML)
    assert out["consensus_label"] == "Strong Buy"
    assert out["average_price_target"] == pytest.approx(1100.50)
    assert out["num_buy"] == 30
    assert out["num_hold"] == 5
    assert out["num_sell"] == 1


def test_parse_analyst_consensus_unparsable_raises() -> None:
    with pytest.raises(MissingDataSourceError):
        _parse_analyst_consensus("<html><body>nothing here</body></html>")


def test_parse_blogger_sentiment_from_next_data() -> None:
    out = _parse_blogger_sentiment(_next_data_html(_BLOGGER_PAYLOAD))
    assert out["bullish_pct"] == pytest.approx(78.0)
    assert out["bearish_pct"] == pytest.approx(22.0)


def test_parse_blogger_sentiment_from_fallback_html() -> None:
    out = _parse_blogger_sentiment(_FALLBACK_BLOGGER_HTML)
    assert out["bullish_pct"] == pytest.approx(78.0)
    assert out["bearish_pct"] == pytest.approx(22.0)


def test_parse_blogger_sentiment_unparsable_raises() -> None:
    with pytest.raises(MissingDataSourceError):
        _parse_blogger_sentiment("<html></html>")


def test_parse_hedge_fund_signal_from_next_data() -> None:
    out = _parse_hedge_fund_signal(_next_data_html(_HEDGE_PAYLOAD))
    assert out["hedge_funds_holding"] == 84
    assert out["recent_change"] == "increased"


def test_parse_hedge_fund_signal_from_fallback_html() -> None:
    out = _parse_hedge_fund_signal(_FALLBACK_HEDGE_HTML)
    assert out["hedge_funds_holding"] == 84
    assert out["recent_change"] == "increased"


def test_parse_hedge_fund_signal_unparsable_raises() -> None:
    with pytest.raises(MissingDataSourceError):
        _parse_hedge_fund_signal("<html></html>")


# ----------------------------------------------------------------------
# Adapter tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_analyst_consensus_caches(engine: None) -> None:
    html = _next_data_html(_CONSENSUS_PAYLOAD)
    fake = _FakeHttp(html)
    adapter = TipRanksAdapter(http_client=fake)
    out1 = await adapter.get_analyst_consensus("NVDA")
    out2 = await adapter.get_analyst_consensus("NVDA")
    assert out1 == out2
    assert len(fake.calls) == 1
    assert out1["ticker"] == "NVDA"
    assert out1["consensus_label"] == "Strong Buy"


@pytest.mark.asyncio
async def test_get_blogger_sentiment(engine: None) -> None:
    html = _next_data_html(_BLOGGER_PAYLOAD)
    adapter = TipRanksAdapter(http_client=_FakeHttp(html))
    out = await adapter.get_blogger_sentiment("NVDA")
    assert out["bullish_pct"] == pytest.approx(78.0)


@pytest.mark.asyncio
async def test_get_hedge_fund_signal(engine: None) -> None:
    html = _next_data_html(_HEDGE_PAYLOAD)
    adapter = TipRanksAdapter(http_client=_FakeHttp(html))
    out = await adapter.get_hedge_fund_signal("NVDA")
    assert out["hedge_funds_holding"] == 84
    assert out["recent_change"] == "increased"


@pytest.mark.asyncio
async def test_outage_raises(engine: None) -> None:
    adapter = TipRanksAdapter(http_client=_FailingHttp())
    with pytest.raises(MissingDataSourceError):
        await adapter.get_analyst_consensus("NVDA")


@pytest.mark.asyncio
async def test_bad_status_raises(engine: None) -> None:
    adapter = TipRanksAdapter(http_client=_FakeHttp("oops", status=503))
    with pytest.raises(MissingDataSourceError):
        await adapter.get_analyst_consensus("NVDA")


@pytest.mark.asyncio
async def test_invalid_inputs(engine: None) -> None:
    adapter = TipRanksAdapter(http_client=_FailingHttp())
    with pytest.raises(ValueError):
        await adapter.get_analyst_consensus("")
    with pytest.raises(ValueError):
        await adapter.get_blogger_sentiment("")
    with pytest.raises(ValueError):
        await adapter.get_hedge_fund_signal("")


# ----------------------------------------------------------------------
# T3.2 fallback tests — TipRanks → Finnhub social-sentiment
# ----------------------------------------------------------------------


class _FakeFinnhubAdapter:
    """Minimal stand-in for the Finnhub adapter that respects the
    ``get_social_sentiment`` contract our TipRanks fallback expects."""

    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._payload = payload
        self._raise = raise_exc
        self.calls: list[str] = []

    async def get_social_sentiment(self, ticker: str) -> dict[str, Any]:
        # Mirror the real adapter's outcome-tracking so tests that
        # inspect ``collect_outcomes()`` see both rows.
        from argosy.services.adapter_outcomes import track_adapter_call

        with track_adapter_call("finnhub_social", target=ticker) as _outcome:
            self.calls.append(ticker)
            if self._raise is not None:
                raise self._raise
            assert self._payload is not None  # test must set one
            _outcome.set_payload_size_bytes(len(json.dumps(self._payload)))
            return self._payload


@pytest.mark.asyncio
async def test_tipranks_succeeds_no_fallback(engine: None) -> None:
    from argosy.services.adapter_outcomes import collect_outcomes, reset_outcomes

    reset_outcomes()
    html = _next_data_html(_BLOGGER_PAYLOAD)
    finnhub = _FakeFinnhubAdapter(payload={"should": "not be used"})
    adapter = TipRanksAdapter(http_client=_FakeHttp(html), finnhub=finnhub)
    out = await adapter.get_blogger_sentiment("NVDA")
    assert out["bullish_pct"] == pytest.approx(78.0)
    assert out["bearish_pct"] == pytest.approx(22.0)
    # Finnhub must NOT have been called on the happy path.
    assert finnhub.calls == []
    outcomes = collect_outcomes()
    # Only TipRanks's "ok" outcome should be recorded.
    assert any(
        o.adapter_name == "tipranks" and o.status == "ok"
        for o in outcomes
    )
    assert not any(o.adapter_name == "finnhub_social" for o in outcomes)


@pytest.mark.asyncio
async def test_tipranks_falls_back_to_finnhub_on_403(engine: None) -> None:
    from argosy.services.adapter_outcomes import collect_outcomes, reset_outcomes

    reset_outcomes()
    finnhub = _FakeFinnhubAdapter(payload={
        "ticker": "NVDA",
        "bullish_pct": 65.5,
        "bearish_pct": 34.5,
        "source_url": "https://finnhub.io/api/v1/stock/social-sentiment?symbol=NVDA",
    })
    adapter = TipRanksAdapter(
        http_client=_FakeHttp("Forbidden", status=403),
        finnhub=finnhub,
    )
    out = await adapter.get_blogger_sentiment("NVDA")
    # Mapped Finnhub data is returned with the TipRanks dict shape.
    assert out["ticker"] == "NVDA"
    assert out["bullish_pct"] == pytest.approx(65.5)
    assert out["bearish_pct"] == pytest.approx(34.5)
    assert "finnhub.io" in out["source_url"]
    # Finnhub was called exactly once with the normalized ticker.
    assert finnhub.calls == ["NVDA"]
    outcomes = collect_outcomes()
    # Both outcomes recorded: TipRanks=http_error(403), Finnhub=ok.
    tipranks_outcomes = [o for o in outcomes if o.adapter_name == "tipranks"]
    finnhub_outcomes = [o for o in outcomes if o.adapter_name == "finnhub_social"]
    assert len(tipranks_outcomes) == 1
    assert tipranks_outcomes[0].status == "http_error"
    assert tipranks_outcomes[0].http_status_code == 403
    assert len(finnhub_outcomes) == 1
    assert finnhub_outcomes[0].status == "ok"


@pytest.mark.asyncio
async def test_tipranks_falls_back_to_finnhub_on_500(engine: None) -> None:
    from argosy.services.adapter_outcomes import collect_outcomes, reset_outcomes

    reset_outcomes()
    finnhub = _FakeFinnhubAdapter(payload={
        "ticker": "AAPL",
        "bullish_pct": 50.0,
        "bearish_pct": 50.0,
        "source_url": "https://finnhub.io/api/v1/stock/social-sentiment?symbol=AAPL",
    })
    adapter = TipRanksAdapter(
        http_client=_FakeHttp("Internal Server Error", status=500),
        finnhub=finnhub,
    )
    out = await adapter.get_blogger_sentiment("AAPL")
    assert out["bullish_pct"] == pytest.approx(50.0)
    assert finnhub.calls == ["AAPL"]
    outcomes = collect_outcomes()
    tipranks_outcomes = [o for o in outcomes if o.adapter_name == "tipranks"]
    assert tipranks_outcomes[0].status == "http_error"
    assert tipranks_outcomes[0].http_status_code == 500
    assert any(
        o.adapter_name == "finnhub_social" and o.status == "ok"
        for o in outcomes
    )


@pytest.mark.asyncio
async def test_both_fail(engine: None) -> None:
    """TipRanks 403 + Finnhub 503: return zero-shape default, record both."""
    from argosy.services.adapter_outcomes import collect_outcomes, reset_outcomes
    from argosy.adapters import MissingDataSourceError

    reset_outcomes()
    finnhub = _FakeFinnhubAdapter(
        raise_exc=MissingDataSourceError("finnhub social: HTTP 503"),
    )
    adapter = TipRanksAdapter(
        http_client=_FakeHttp("Forbidden", status=403),
        finnhub=finnhub,
    )
    out = await adapter.get_blogger_sentiment("NVDA")
    # Existing dict shape; zero-valued — caller treats as "no data".
    assert out["ticker"] == "NVDA"
    assert out["bullish_pct"] == 0.0
    assert out["bearish_pct"] == 0.0
    assert out["source_url"] == ""
    outcomes = collect_outcomes()
    # Both outcomes recorded as failures.
    tipranks_outcomes = [o for o in outcomes if o.adapter_name == "tipranks"]
    finnhub_outcomes = [o for o in outcomes if o.adapter_name == "finnhub_social"]
    assert len(tipranks_outcomes) == 1
    assert tipranks_outcomes[0].status == "http_error"
    assert tipranks_outcomes[0].http_status_code == 403
    assert len(finnhub_outcomes) == 1
    assert finnhub_outcomes[0].status == "exception"
