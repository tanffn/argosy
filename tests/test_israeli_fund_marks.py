"""Identity, unit math, dated public NAVs and the real snapshot/DB seam."""
import json
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from argosy.adapters.data.israeli_funds import FundQuote, parse_fund_quote
from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot
from argosy.services.israeli_fund_marks import attach_fund_identity, enrich_from_broker, reprice_fund_position


def page(*, sid="5122569", price=273.87, day=23, currency="שקל", kind="mtf"):
    asset = dict(assetNumber=sid, value=price, name="Test fund", type=kind,
                 lastTradeTime=datetime(2026, 9, day, tzinfo=UTC).timestamp() * 1000)
    state = {f'ROOT_QUERY.assets({{"ids":"5122569"}}).0': asset}
    return ('<script id="__NEXT_DATA__" type="application/json">' +
            json.dumps({"props": {"apolloState": state}}) + '</script>' +
            f'<table><tr><td>מטבע עיקרי</td><td>{currency}</td></tr></table>')


def test_parser_identity_units_and_date():
    q = parse_fund_quote(page(), "5122569", today=date(2026, 9, 26))
    assert q.price_ils == pytest.approx(2.7387)
    assert q.as_of == date(2026, 9, 23)
    assert q.source_url.endswith("/5122569")
    assert len(q.sha256) == 64


@pytest.mark.parametrize("kwargs", [dict(sid="5139951"), dict(kind="stock"),
    dict(price=0), dict(price=float("nan")), dict(currency="דולר"), dict(day=1), dict(day=27)])
def test_parser_rejects_bad_marks(kwargs):
    with pytest.raises(ValueError):
        parse_fund_quote(page(**kwargs), "5122569", today=date(2026, 9, 26))


def position():
    return PortfolioPosition(location="Leumi", symbol="MSCI WORLD", details="Test fund",
        currency="USD", asset_type="Broad Index", shares=40000, current_price=274.4,
        current_value_local=36308.3, usd_value_k=36.3083,
        observed_as_of=date(2026, 9, 11), valued_as_of=date(2026, 9, 11))


def identified():
    return attach_fund_identity(position().model_dump(), security_id="5122569",
                                broker_price=274.4, broker_avg_price=247.37)


def quote(**kwargs):
    return replace(FundQuote("5122569", "Test fund", 2.7387, date(2026, 9, 23),
                            datetime.now(UTC), "https://test/5122569", "abc"), **kwargs)


def test_normalizes_once_keeps_quantity_observation_and_real_mark_date():
    p = identified()
    assert p["current_price"] * p["shares"] == pytest.approx(p["current_value_local"])
    for _ in range(2):
        p = reprice_fund_position(p, fx_usd_nis=3.0, today=date(2026, 9, 26), quote_fn=lambda *a, **kw: quote())
        assert p["current_price"] == pytest.approx(2.7387)
        assert p["current_value_local"] == pytest.approx(109548)
        assert p["usd_value_k"] == pytest.approx(36.516)
        assert p["avg_price"] == pytest.approx(2.4737)
        assert p["shares"] == 40000
        assert p["valued_as_of"] == date(2026, 9, 23)
        assert p["observed_as_of"] == date(2026, 9, 11)
        assert not p["mark_stale"]


@pytest.mark.parametrize("q", [quote(price_ils=273.87), quote(price_ils=float("nan")),
    quote(security_id="5139951"), quote(as_of=date(2026, 9, 1)), quote(as_of=date(2026, 9, 27))])
def test_mark_math_fails_closed(q):
    assert reprice_fund_position(identified(), fx_usd_nis=3, today=date(2026, 9, 26),
                                quote_fn=lambda *a, **kw: q) is None


def test_enrichment_does_not_guess_or_overwrite_quantity():
    from argosy.adapters.brokers.leumi_portfolio_xls import LeumiHolding
    h = LeumiHolding("5122569", "Test fund", None, None, 40000, 247.37, 274.4, 36308.3, None, None)
    p = position()
    enriched = enrich_from_broker([p], [h], source="bank.xls")[0]
    assert enriched.security_id == h.security_id
    assert enriched.identity_source == "bank.xls"
    assert enrich_from_broker([p], [replace(h, name="Other fund")], source="bank.xls")[0] == p
    with pytest.raises(ValueError, match="quantity mismatch"):
        enrich_from_broker([p], [replace(h, quantity=10)], source="bank.xls")


def test_public_provider_is_cached_and_validated(monkeypatch):
    from argosy.adapters.data import israeli_funds as adapter
    import httpx
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, text=page(), request=httpx.Request("GET", url))
    monkeypatch.setattr(adapter, "_cache", {})
    monkeypatch.setattr(adapter.httpx, "get", get)
    q = adapter.get_fund_quote("5122569", today=date(2026, 9, 26))
    assert adapter.get_fund_quote("5122569", today=date(2026, 9, 26)) == q
    assert len(calls) == 1
    with pytest.raises(ValueError):
        adapter.get_fund_quote("../other", today=date(2026, 9, 26))
    # A cached quote cannot become eligible on a much later date.
    with pytest.raises(ValueError, match="ineligible"):
        adapter.get_fund_quote("5122569", today=date(2026, 10, 26))


def test_provider_long_retry_after_is_not_hammered(monkeypatch):
    from argosy.adapters.data import israeli_funds as adapter
    import httpx
    calls = []
    clock = [1000.0]
    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(429, headers={"Retry-After": "3600"}, request=httpx.Request("GET", url))
    monkeypatch.setattr(adapter, "_cache", {})
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(adapter.httpx, "get", get)
    with pytest.raises(httpx.HTTPStatusError):
        adapter.get_fund_quote("5122569")
    clock[0] += 600
    with pytest.raises(ValueError):
        adapter.get_fund_quote("5122569")
    assert len(calls) == 1


def test_broker_tsv_roundtrip_keeps_units_identity_and_fractional_quantity():
    from types import SimpleNamespace
    from argosy.services.portfolio_ingest.xls_osh_pair import _xls_to_tsv_rows, _position_to_cells
    from argosy.ingest.tsv import _parse_position_row
    broker = SimpleNamespace(security_id="5122569", name_he="Test fund", ticker=None,
        quantity=40000.1234, last_price=274.4, avg_buy_price=247.37, gain_pct=None,
        usd_value=lambda fx: 36308.3)
    xls = SimpleNamespace(positions=[broker], snapshot_date=date(2026, 9, 11))
    cells = _xls_to_tsv_rows(xls=xls, osh_closing_nis=0, fx_usd_nis=3,
        symbol_map={broker.security_id: "MSCI WORLD"}, currency_map={broker.security_id: "ILS"},
        type_map={broker.security_id: "Broad Index"})[-1].split("\t")
    p = _parse_position_row(cells, line_no=1)
    assert p.security_id == "5122569" and p.quote_market == "TASE_FUND"
    assert p.shares == 40000.1234
    assert p.current_value_local == pytest.approx(36308.3 * 3)
    assert p.current_price * p.shares == pytest.approx(p.current_value_local)
    assert p.broker_price == 274.4
    assert p.avg_price is None
    assert p.valued_as_of == date(2026, 9, 11)
    for _ in range(2):
        p = _parse_position_row(_position_to_cells(p), line_no=1)
        assert p.security_id == "5122569" and p.shares == 40000.1234
        assert p.current_price * p.shares == pytest.approx(p.current_value_local)


def test_canonical_nav_tsv_roundtrip_keeps_date_and_quote_basis():
    from argosy.services.portfolio_ingest.xls_osh_pair import _position_to_cells
    from argosy.ingest.tsv import _parse_position_row
    p = PortfolioPosition(**reprice_fund_position(identified(), fx_usd_nis=3,
        today=date(2026, 9, 26), quote_fn=lambda *a, **kw: quote()))
    for _ in range(2):
        p = _parse_position_row(_position_to_cells(p), line_no=1)
        assert p.current_price == 2.7387
        assert p.currency == "ILS"
        assert p.security_id == "5122569"
        assert p.valued_as_of == date(2026, 9, 23)
        assert p.observed_as_of == date(2026, 9, 11)
        assert p.price_source == "https://test/5122569"
    assert reprice_fund_position(p.model_dump(), fx_usd_nis=3, today=date(2026, 9, 26),
        quote_fn=lambda *a, **kw: quote(as_of=date(2026, 9, 17))) is None


@pytest.mark.real_seam
def test_real_snapshot_and_book_path(tmp_path, monkeypatch):
    # Real DB, persistence, total-book merge, and refresh; only network is injected.
    from argosy.state.models import Base
    from argosy.services.portfolio_snapshot_store import persist_snapshot, row_to_snapshot
    from argosy.services.snapshot_refresh import refresh_portfolio_snapshot
    from argosy.services.holding_books import merge_total_book_positions
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'fund.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("argosy.adapters.data.israeli_funds.get_fund_quote", lambda *a, **kw: quote())
    with Session(engine, expire_on_commit=False) as session:
        p = PortfolioPosition(**identified())
        snap = PortfolioSnapshot(source_path="bank.xls", snapshot_date=date(2026, 9, 11),
                                 fx_usd_nis=3, positions=[p])
        persist_snapshot(session, user_id="test", snapshot=snap)
        res = refresh_portfolio_snapshot(session, user_id="test", today=date(2026, 9, 26),
                                         fx_fn=lambda: {"usd_nis": 3})
        saved = row_to_snapshot(res.row).positions[0]
        assert saved.security_id == "5122569"
        assert saved.valued_as_of == date(2026, 9, 23)
        assert saved.usd_value_k == pytest.approx(36.516)
        failures, stale = [], []
        book = merge_total_book_positions([p], today=date(2026, 9, 26), fx_usd_nis=3,
                                           reprice_failures=failures, stale_marks=stale)
        assert not failures and not stale
        assert book[0]["usd_value_k"] == pytest.approx(36.516)
        # A fetched 9-day-old NAV stays 9 days old, never laundered into today.
        monkeypatch.setattr("argosy.adapters.data.israeli_funds.get_fund_quote",
                            lambda *a, **kw: quote(as_of=date(2026, 9, 17)))
        book = merge_total_book_positions([p], today=date(2026, 9, 26), fx_usd_nis=3,
                                           reprice_failures=failures, stale_marks=stale)
        assert book[0]["mark_stale"] and stale
        assert book[0]["valued_as_of"] == date(2026, 9, 17)
        # Network failure retains the old date and a warning on the scheduled path.
        def fail(*a, **kw): raise ValueError("provider unavailable")
        monkeypatch.setattr("argosy.adapters.data.israeli_funds.get_fund_quote", fail)
        res = refresh_portfolio_snapshot(session, user_id="test", today=date(2026, 9, 27),
                                         fx_fn=lambda: {"usd_nis": 3})
        assert res.snapshot.positions[0].valued_as_of == date(2026, 9, 23)
        assert res.snapshot.positions[0].mark_stale
        assert any("israeli-fund-nav" in w for w in res.warnings)
    engine.dispose()
