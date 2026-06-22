"""Tests for the deterministic Stage-1 routing policy."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from argosy.services.decision_funnel.book import BookHolding
from argosy.services.decision_funnel.policy import RoutingPolicy, should_audit_drop
from argosy.services.decision_funnel.stage0_market import MarketRead
from argosy.services.decision_funnel.stage1_routing import PerNameSignal, route

NOW = datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc)
DAY = "2026-06-22"


def _market(risk_off=False, summary="calm"):
    return MarketRead(
        as_of=NOW.isoformat(), macro_tone=None, macro_tone_confidence=None,
        risk_off=risk_off, vix=None, vix_band=None, key_themes=[],
        ticker_signals=[], high_materiality_news=[], source_refs=[], summary=summary,
    )


def _ips_stub(**overrides):
    """Minimal IPS-like stub exposing .value on the fields route() reads."""
    def f(v):
        return SimpleNamespace(value=v, status="resolved")
    base = dict(
        nvda_cap_pct=f(13.0),
        general_single_name_cap_pct=f(10.0),
        nvda_target_pct=f(12.0),
        sell_trigger_drift_pct=f(5.0),
        sleeve_targets=[
            SimpleNamespace(label="US core", sigma_class="growth", target_pct=40.0),
            SimpleNamespace(label="Bonds", sigma_class="bonds", target_pct=15.0),
        ],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _book(*pairs):
    return [BookHolding(ticker=t, asset_type="stock", usd_value_k=w * 10, weight_pct=w) for t, w in pairs]


def test_thesis_broken_routes_even_in_cooldown():
    book = _book(("AAPL", 5.0))
    sig = {"AAPL": PerNameSignal(ticker="AAPL", thesis_status="broken", thesis_severity="critical")}
    res = route(
        book=book, market_read=_market(), ips=_ips_stub(), signals=sig,
        last_review_by_ticker={"AAPL": NOW - timedelta(days=1)},  # in cooldown
        day=DAY, now=NOW,
    )
    assert len(res.routed) == 1
    assert "thesis_broken" in res.routed[0].triggers


def test_big_move_routes():
    book = _book(("AAPL", 5.0))
    sig = {"AAPL": PerNameSignal(ticker="AAPL", ret_1m_pct=-22.0)}
    res = route(book=book, market_read=_market(), ips=_ips_stub(), signals=sig, day=DAY, now=NOW)
    assert "big_move" in res.routed[0].triggers


def test_earnings_imminent_routes():
    book = _book(("AAPL", 5.0))
    sig = {"AAPL": PerNameSignal(ticker="AAPL", earnings_in_days=3)}
    res = route(book=book, market_read=_market(), ips=_ips_stub(), signals=sig, day=DAY, now=NOW)
    assert "earnings_imminent" in res.routed[0].triggers


def test_concentration_cap_breach_routes():
    book = _book(("NVDA", 60.0))  # >> 13% cap
    res = route(book=book, market_read=_market(), ips=_ips_stub(), signals={}, day=DAY, now=NOW)
    assert any("concentration_cap_breach" in c.triggers for c in res.routed)


def test_nvda_drift_band_breach_routes():
    book = _book(("NVDA", 12.0 + 6.0))  # 6pp above 12% target, band 5pp
    # weight 18 also > 13 cap, so cap fires too; assert drift present.
    res = route(book=book, market_read=_market(), ips=_ips_stub(), signals={}, day=DAY, now=NOW)
    assert any("drift_band_breach" in c.triggers for c in res.routed)


def test_general_cap_fallback_when_ips_none_routes_unverified():
    # ips=None -> the real cap is unknown, so an above-fallback weight routes as
    # 'concentration_unverified' (conservative: never a silent drop).
    book = _book(("AAPL", 12.0))  # > 10% general fallback cap
    res = route(book=book, market_read=_market(), ips=None, signals={}, day=DAY, now=NOW)
    assert any("concentration_unverified" in c.triggers for c in res.routed)


def test_high_materiality_news_routes_when_not_in_cooldown():
    book = _book(("AAPL", 5.0))
    sig = {"AAPL": PerNameSignal(ticker="AAPL", high_materiality_news=True, news_sentiment="bearish")}
    res = route(book=book, market_read=_market(), ips=_ips_stub(), signals=sig, day=DAY, now=NOW)
    assert res.routed and res.routed[0].primary_signal == "high_materiality_news"


def test_high_materiality_news_bypasses_cooldown():
    # codex BLOCKER 2: a fresh high-materiality item must route even if the name
    # was deep-reviewed recently — it must not be suppressed by cooldown.
    book = _book(("AAPL", 5.0))
    sig = {"AAPL": PerNameSignal(ticker="AAPL", high_materiality_news=True)}
    res = route(
        book=book, market_read=_market(), ips=_ips_stub(), signals=sig,
        last_review_by_ticker={"AAPL": NOW - timedelta(days=1)},  # in cooldown
        day=DAY, now=NOW,
    )
    assert res.routed and res.routed[0].primary_signal == "high_materiality_news"


def test_ordinary_name_held_in_cooldown():
    book = _book(("AAPL", 5.0))
    pol = RoutingPolicy(audit_drop_one_in=0)
    res = route(
        book=book, market_read=_market(), ips=_ips_stub(), signals={},
        last_review_by_ticker={"AAPL": NOW - timedelta(days=1)},
        policy=pol, day=DAY, now=NOW,
    )
    assert not res.routed
    assert res.dropped and "cooldown" in res.dropped[0].reason


def test_market_read_news_merged_without_signal_dict():
    # codex BLOCKER 1: news from the Stage-0 read routes even when the caller
    # didn't duplicate it into `signals`.
    from argosy.services.decision_funnel.stage0_market import MarketRead, NewsHit

    book = _book(("AAPL", 5.0))
    mkt = MarketRead(
        as_of=NOW.isoformat(), macro_tone=None, macro_tone_confidence=None,
        risk_off=False, vix=None, vix_band=None, key_themes=[], ticker_signals=[],
        high_materiality_news=[NewsHit(1, "AAPL", "bearish", "high", "x")],
        source_refs=[], summary="x",
    )
    res = route(book=book, market_read=mkt, ips=_ips_stub(), signals={}, day=DAY, now=NOW)
    assert res.routed and res.routed[0].primary_signal == "high_materiality_news"


def test_pending_nvda_cap_routes_unverified_not_drop():
    # codex BLOCKER 3: a pending NVDA cap must not cause a silent drop; an
    # above-fallback weight routes as 'concentration_unverified'.
    from types import SimpleNamespace

    ips = _ips_stub(nvda_cap_pct=SimpleNamespace(value=None, status="pending"))
    book = _book(("NVDA", 14.0))  # > 13 fallback, real cap unknown
    res = route(book=book, market_read=_market(), ips=ips, signals={}, day=DAY, now=NOW)
    assert any("concentration_unverified" in c.triggers for c in res.routed)


def test_weakened_warning_routes():
    book = _book(("AAPL", 5.0))
    sig = {"AAPL": PerNameSignal(ticker="AAPL", thesis_status="weakened", thesis_severity="warning")}
    res = route(book=book, market_read=_market(), ips=_ips_stub(), signals=sig, day=DAY, now=NOW)
    assert "thesis_weakened" in res.routed[0].triggers


def test_no_signal_blind_drop():
    book = _book(("AAPL", 5.0))
    pol = RoutingPolicy(audit_drop_one_in=0, blind_drop_audit_one_in=0)
    res = route(book=book, market_read=_market(), ips=_ips_stub(), signals={}, policy=pol, day=DAY, now=NOW)
    assert not res.routed
    assert "no signal coverage" in res.dropped[0].reason


def test_no_material_signal_drop_when_partial_coverage():
    # A name WITH some signal coverage (a price read) but nothing material
    # drops as "no material signal", not "blind".
    book = _book(("AAPL", 5.0))
    sig = {"AAPL": PerNameSignal(ticker="AAPL", ret_1m_pct=2.0)}
    pol = RoutingPolicy(audit_drop_one_in=0, blind_drop_audit_one_in=0)
    res = route(book=book, market_read=_market(), ips=_ips_stub(), signals=sig, policy=pol, day=DAY, now=NOW)
    assert res.dropped[0].reason == "no material signal"


def test_audit_reroutes_drops_when_rate_is_one():
    book = _book(("AAPL", 5.0), ("MSFT", 4.0))
    pol = RoutingPolicy(audit_drop_one_in=1, blind_drop_audit_one_in=1)  # every drop audited
    res = route(book=book, market_read=_market(), ips=_ips_stub(), signals={}, policy=pol, day=DAY, now=NOW)
    assert not res.dropped
    assert all(c.is_audit for c in res.routed if c.subject_type == "holding")
    assert {c.subject for c in res.routed} == {"AAPL", "MSFT"}


def test_risk_off_routes_equity_sleeves_only():
    book = _book(("AAPL", 5.0))
    pol = RoutingPolicy(audit_drop_one_in=0)
    res = route(
        book=book, market_read=_market(risk_off=True, summary="risk-off"),
        ips=_ips_stub(), signals={}, policy=pol, day=DAY, now=NOW,
    )
    sleeves = [c for c in res.routed if c.subject_type == "sleeve"]
    assert sleeves
    # Only the growth sleeve, not bonds.
    assert any("US core" in c.subject for c in sleeves)
    assert not any("Bonds" in c.subject for c in sleeves)


def test_policy_version_is_stable_and_sensitive():
    v1 = RoutingPolicy().version
    v2 = RoutingPolicy().version
    v3 = RoutingPolicy(cooldown_days=10).version
    assert v1 == v2 and v1 != v3
    assert v1.startswith("pol-")


def test_should_audit_drop_deterministic():
    pol = RoutingPolicy(audit_drop_one_in=25)
    a = should_audit_drop(pol, day=DAY, ticker="AAPL")
    b = should_audit_drop(pol, day=DAY, ticker="AAPL")
    assert a == b  # reproducible
